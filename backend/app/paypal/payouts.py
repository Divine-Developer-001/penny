"""Money-out layer: PayPal sandbox Payouts (create, poll) and reconciliation into the ledger."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ledger
from app.config import Settings
from app.models import Payout, PayoutStatus
from app.paypal.client import PayPalClient, PayPalError
from app.schemas import usd

logger = logging.getLogger(__name__)
NEGATIVE_TEST_NOTE = "ERRPYO002"          # sandbox negative-test value -> SENDER_EMAIL_UNCONFIRMED

_COMPLETED = {"SUCCESS"}
_FAILED = {"FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED"}


def map_item_status(transaction_status: str) -> str:
    s = (transaction_status or "").upper()
    if s in _COMPLETED:
        return PayoutStatus.COMPLETED
    if s in _FAILED:
        return PayoutStatus.FAILED
    return PayoutStatus.IN_TRANSIT            # PENDING, ONHOLD, UNCLAIMED, ...


def find_item_id(client: PayPalClient, batch_id: str) -> str | None:
    data = client.request("GET", f"/v1/payments/payouts/{batch_id}")
    items = data.get("items") or []
    return items[0].get("payout_item_id") if items else None


def create_payout(client: PayPalClient, settings: Settings, amount: Decimal, *, note: str | None = None) -> dict[str, Any]:
    """POST /v1/payments/payouts with one EMAIL item. Returns the ids needed to track it."""
    amount = usd(amount)
    sender_batch_id, sender_item_id = f"penny-{uuid.uuid4().hex}", f"penny-item-{uuid.uuid4().hex}"
    body = {
        "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "Your Penny withdrawal (sandbox)",
                                "email_message": "Penny sandbox payout."},
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": f"{amount:.2f}", "currency": "USD"},
            "receiver": settings.paypal_payout_receiver_email,
            "note": NEGATIVE_TEST_NOTE if settings.paypal_negative_test else (note or "Penny withdrawal (sandbox)"),
            "sender_item_id": sender_item_id,
            "recipient_wallet": "PAYPAL",
        }],
    }
    resp = client.request("POST", "/v1/payments/payouts", json_body=body)
    batch_id = (resp.get("batch_header") or {}).get("payout_batch_id")
    item_id = None
    if batch_id:
        try:
            item_id = find_item_id(client, batch_id)
        except PayPalError as exc:
            logger.info("could not look up payout item yet: %s", exc)
    return {"payout_batch_id": batch_id, "sender_batch_id": sender_batch_id,
            "sender_item_id": sender_item_id, "payout_item_id": item_id}


def get_payout_item(client: PayPalClient, item_id: str) -> dict[str, Any]:
    return client.request("GET", f"/v1/payments/payouts-item/{item_id}")


def poll_payouts(session: Session, client: PayPalClient) -> int:
    """Refresh every unsettled payout from PayPal (webhooks are the fast path, this is the backstop)."""
    open_payouts = list(session.scalars(select(Payout).where(
        Payout.status.in_((PayoutStatus.CREATED, PayoutStatus.IN_TRANSIT)))))
    changed = 0
    for payout in open_payouts:
        try:
            if not payout.paypal_payout_item_id and payout.paypal_batch_id:
                payout.paypal_payout_item_id = find_item_id(client, payout.paypal_batch_id)
            if not payout.paypal_payout_item_id:
                continue
            item = get_payout_item(client, payout.paypal_payout_item_id)
            new = map_item_status(item.get("transaction_status", ""))
            if new == PayoutStatus.COMPLETED:
                ledger.complete_payout(session, payout)
            elif new == PayoutStatus.FAILED:
                err = (item.get("errors") or {}).get("name") or item.get("transaction_status", "failed")
                ledger.fail_payout(session, payout, str(err))
            elif payout.status != new:
                payout.status = new
            else:
                continue
            changed += 1
        except PayPalError as exc:
            logger.warning("polling payout %s failed: %s", payout.id, exc)
    return changed
