"""Money-in layer: store checkout. POST /api/orders creates a PayPal sandbox order at the SERVER-side price;
POST /api/orders/{id}/capture captures it and credits simulated cashback (idempotent with the webhook)."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cashback, ledger, prices, threshold
from app.api import Services, get_services, get_session
from app.models import CashbackCredit, Product, Purchase
from app.paypal import orders as paypal_orders
from app.paypal.client import PayPalError
from app.schemas import CaptureOut, CreateOrderIn, CreateOrderOut, Rules, usd, utcnow

logger = logging.getLogger(__name__)
router = APIRouter()
STORE = "Penny Shop"


@router.post("/orders", response_model=CreateOrderOut)
def create_order(body: CreateOrderIn, services: Services = Depends(get_services),
                 session: Session = Depends(get_session)) -> CreateOrderOut:
    product = session.get(Product, body.product_id)
    if product is None:
        raise HTTPException(404, "unknown product")
    if body.mode == "paypal" and not services.settings.paypal_configured:
        raise HTTPException(503, "PayPal sandbox credentials are not configured; use the simulated checkout")
    purchase = Purchase(product_id=product.id, product_name=product.name, merchant=STORE, price=usd(product.price),
                        coupon_code="", cashback_rate=services.settings.cashback_rate, mode=body.mode,
                        order_id=f"pending-{uuid.uuid4().hex}", created_at=utcnow())
    session.add(purchase)
    session.flush()
    if body.mode == "paypal":
        try:
            purchase.order_id = paypal_orders.create_order(services.paypal, purchase.price, reference_id=f"penny-{purchase.id}",
                                                           description=product.name)
        except PayPalError as exc:
            session.rollback()
            raise HTTPException(502, f"PayPal could not create the order: {exc}")
    else:
        purchase.order_id = f"SIM-{uuid.uuid4().hex[:16].upper()}"
    session.commit()
    return CreateOrderOut(order_id=purchase.order_id, mode=purchase.mode, amount=purchase.price, merchant=purchase.merchant,
                          product_name=purchase.product_name, cashback_rate=purchase.cashback_rate)


def _maybe_start_agent(services: Services, background: BackgroundTasks) -> None:
    """Run the agent right after a purchase, but only when the pot is actually ready to invest."""
    if not services.settings.auto_agent_on_purchase:
        return
    from app.scheduler import run_agent_job
    with services.session_factory() as s:
        profile = ledger.get_profile(s, services.settings)
        held = [p.ticker for p in ledger.get_positions(s)]
        quotes = prices.get_quotes(services.broker, list(profile.targets) + held)
        snap = ledger.build_snapshot(s, quotes, utcnow())
        if threshold.decide(snap, profile, Rules.from_settings(services.settings)).invest:
            background.add_task(run_agent_job, services)


@router.post("/orders/{order_id}/capture", response_model=CaptureOut)
def capture_order(order_id: str, background: BackgroundTasks, services: Services = Depends(get_services),
                  session: Session = Depends(get_session)) -> CaptureOut:
    purchase = session.scalar(select(Purchase).where(Purchase.order_id == order_id))
    if purchase is None:
        raise HTTPException(404, "unknown order")
    if purchase.status == "CAPTURED":
        return CaptureOut(status="COMPLETED", amount=purchase.price, cashback=purchase.cashback_amount,
                          cashback_rate=purchase.cashback_rate, message="This order was already paid.", duplicate=True)
    if purchase.mode == "paypal":
        try:
            info = paypal_orders.capture_order(services.paypal, order_id)
        except PayPalError as exc:
            purchase.status = "FAILED"
            session.commit()
            raise HTTPException(402, f"PayPal could not capture the payment: {exc}")
        if not info.completed:
            raise HTTPException(409, f"payment is not completed yet (order {info.order_status or '?'}, capture {info.capture_status or '?'})")
        capture_id, amount = info.capture_id, info.amount
    else:
        capture_id, amount = f"SIMCAP-{uuid.uuid4().hex[:12].upper()}", purchase.price
    if usd(amount) != purchase.price:
        purchase.status = "FAILED"
        session.commit()
        raise HTTPException(409, "captured amount does not match the order")

    key = f"capture:{capture_id}"               # the PAYMENT.CAPTURE.COMPLETED webhook uses the same key: never double-credited
    credit = cashback.apply(session, services.settings, key, amount, rate=purchase.cashback_rate)
    existing = credit or session.scalar(select(CashbackCredit).where(CashbackCredit.paypal_event_id == key))
    purchase.status, purchase.capture_id = "CAPTURED", capture_id
    purchase.cashback_amount = existing.cashback_amount if existing else Decimal("0")
    ledger.snapshot_now(session, services.broker)
    session.commit()
    _maybe_start_agent(services, background)
    return CaptureOut(status="COMPLETED", amount=purchase.price, cashback=purchase.cashback_amount,
                      cashback_rate=purchase.cashback_rate, duplicate=credit is None,
                      message=f"Cashback credited: ${purchase.cashback_amount} (simulated)")
