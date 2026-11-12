"""Money-in layer: POST /webhooks/paypal. Verify signature -> dedupe by event id -> route."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import cashback, ledger
from app.api import Services, get_services
from app.models import Payout, ProcessedWebhook
from app.paypal.webhooks import verify_webhook
from app.schemas import utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

CAPTURE_EVENT = "PAYMENT.CAPTURE.COMPLETED"
PAYOUT_OK = {"PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUT.ITEM.COMPLETED"}
PAYOUT_BAD = {"PAYMENT.PAYOUTS-ITEM.FAILED", "PAYMENT.PAYOUTS-ITEM.BLOCKED", "PAYMENT.PAYOUTS-ITEM.DENIED",
              "PAYMENT.PAYOUTS-ITEM.CANCELED", "PAYMENT.PAYOUTS-ITEM.RETURNED", "PAYMENT.PAYOUTS-ITEM.REFUNDED",
              "PAYOUT.ITEM.FAILED"}


def _find_payout(session: Session, resource: dict[str, Any]) -> Payout | None:
    sender_item = (resource.get("payout_item") or {}).get("sender_item_id")
    for column, value in ((Payout.paypal_payout_item_id, resource.get("payout_item_id")),
                          (Payout.sender_item_id, sender_item),
                          (Payout.paypal_batch_id, resource.get("payout_batch_id"))):
        if value:
            found = session.scalar(select(Payout).where(column == value))
            if found:
                return found
    return None


def handle_event(session: Session, services: Services, headers: dict[str, str], raw: bytes) -> tuple[int, dict[str, str]]:
    result = verify_webhook(headers, raw, services.settings, services.paypal)
    if not result.ok:
        logger.warning("webhook rejected: %s", result.reason)
        return 401, {"status": "rejected", "detail": result.reason}
    try:
        event = json.loads(raw)
        event_id, event_type = str(event["id"]), str(event.get("event_type", ""))
    except (ValueError, KeyError, TypeError):
        return 400, {"status": "bad_request", "detail": "body is not a PayPal event"}
    resource = event.get("resource") or {}

    # Mock events from the simulator reuse sample ids, so their key includes the transmission id.
    lower = {k.lower(): v for k, v in headers.items()}
    key = f"{event_id}:{lower.get('paypal-transmission-id', '')}" if result.source == "simulator" else event_id
    try:
        session.add(ProcessedWebhook(event_key=key, event_type=event_type, source=result.source, created_at=utcnow()))
        session.flush()
    except IntegrityError:
        session.rollback()
        return 200, {"status": "duplicate", "detail": key}

    if event_type == CAPTURE_EVENT:
        amount = resource.get("amount") or {}
        value = amount.get("value") or amount.get("total")
        if value is None or str(amount.get("currency_code") or amount.get("currency") or "USD").upper() != "USD":
            ledger.log_event(session, "webhook_ignored", f"Ignored capture event {event_id}: no USD amount.", {"event": event_id})
            detail = "ignored"
        else:
            cap_id = resource.get("id")      # the web checkout credits under the same capture key
            credit_key = f"capture:{cap_id}" if cap_id and result.source != "simulator" else key
            credit = cashback.apply(session, services.settings, credit_key, value)
            detail = "credited" if credit else "duplicate"
    elif event_type in PAYOUT_OK | PAYOUT_BAD:
        payout = _find_payout(session, resource)
        if payout is None:
            ledger.log_event(session, "webhook_ignored", f"Payout event {event_type} did not match any withdrawal.", {"event": event_id})
            detail = "unmatched"
        elif event_type in PAYOUT_OK:
            ledger.complete_payout(session, payout)
            detail = "payout_completed"
        else:
            reason = (resource.get("errors") or {}).get("name") or resource.get("transaction_status") or event_type
            ledger.fail_payout(session, payout, str(reason))
            detail = "payout_failed"
    else:
        ledger.log_event(session, "webhook_ignored", f"Received {event_type or 'unknown'} event; nothing to do.", {"event": event_id})
        detail = "unhandled"
    session.commit()
    return 200, {"status": "ok", "detail": detail}


def _process(services: Services, headers: dict[str, str], raw: bytes) -> tuple[int, dict[str, str]]:
    session = services.session_factory()
    try:
        return handle_event(session, services, headers, raw)
    except Exception:                                         # noqa: BLE001  non-2xx makes PayPal retry
        session.rollback()
        logger.exception("webhook processing failed")
        return 500, {"status": "error", "detail": "processing failed"}
    finally:
        session.close()


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, services: Services = Depends(get_services)) -> JSONResponse:
    raw = await request.body()                                # raw bytes, before any JSON parsing
    status, body = await run_in_threadpool(_process, services, dict(request.headers), raw)
    return JSONResponse(body, status_code=status)
