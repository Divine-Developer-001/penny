"""Money-out layer: POST /withdraw. Optionally sells investments, then policy check -> pot to in_transit -> PayPal payout."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import explain, ledger, policy
from app.api import Services, get_services, get_session
from app.brokerage.base import BrokerError
from app.models import Payout, PayoutStatus
from app.paypal import payouts
from app.schemas import VerdictStatus, WithdrawOut, WithdrawRequest, usd, utcnow

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/withdraw", response_model=WithdrawOut)
def withdraw(body: WithdrawRequest, services: Services = Depends(get_services),
             session: Session = Depends(get_session)) -> WithdrawOut:
    now, requested = utcnow(), usd(body.amount)
    sold_note = ""
    if body.include_investments and requested > ledger.uncommitted_pot(session):
        try:
            sells = ledger.sell_for_withdrawal(session, services.broker, requested, now)
        except ledger.InsufficientFunds as exc:
            session.rollback()
            raise HTTPException(422, str(exc))
        except (ledger.SellRejected, BrokerError) as exc:
            session.commit()                                  # keep any sells that already executed
            raise HTTPException(409, f"Could not sell investments: {exc}")
        sold_note = f" Sold investments worth {explain.money(sum((s.dollar_amount for s in sells), usd(0)))} to raise cash."
        session.flush()

    amount = min(requested, ledger.uncommitted_pot(session)) if body.include_investments else requested
    snapshot = ledger.build_snapshot(session, {}, now)
    verdict = policy.validate_payout(amount, snapshot, initiated_by_user=True)
    if verdict.status is VerdictStatus.REJECTED:
        ledger.log_event(session, "payout_rejected", explain.rejected(verdict), {"code": verdict.code}, now)
        session.commit()
        raise HTTPException(422, explain.rejected(verdict) + (" Tick 'include my investments' to sell some." if not body.include_investments else ""))

    pending = ledger.move_to_in_transit(session, amount)
    payout = Payout(amount=amount, status=PayoutStatus.CREATED, pending_cash_id=pending.id, created_at=now)
    session.add(payout)
    session.flush()
    try:
        info = payouts.create_payout(services.paypal, services.settings, amount)
    except Exception as exc:                                  # noqa: BLE001
        ledger.fail_payout(session, payout, f"PayPal error: {str(exc)[:160]}")
        session.commit()
        raise HTTPException(502, f"PayPal payout could not be created; your money is back in the pot ({exc})")
    payout.paypal_batch_id, payout.sender_item_id = info["payout_batch_id"], info["sender_item_id"]
    payout.paypal_payout_item_id, payout.status = info["payout_item_id"], PayoutStatus.IN_TRANSIT
    ledger.log_event(session, "payout_started", explain.payout_started(amount), {"payout_id": payout.id, **info}, now)
    ledger.snapshot_now(session, services.broker)
    session.commit()
    return WithdrawOut(payout_id=payout.id, status=payout.status, amount=amount,
                       message="Sandbox payout created; completion arrives by webhook or polling." + sold_note)
