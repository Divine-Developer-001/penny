"""Money-in layer: turns a PayPal capture event into a (simulated) cashback credit in the pot."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import explain, ledger
from app.config import Settings
from app.models import CashbackCredit
from app.schemas import usd, utcnow


def apply(session: Session, settings: Settings, event_key: str, capture_amount: Decimal | str,
          now: datetime | None = None, rate: Decimal | None = None) -> CashbackCredit | None:
    """Credit CASHBACK_RATE (or an explicit per-purchase `rate`) of a captured payment. Idempotent by event key; returns None for a repeat.

    The cashback is SIMULATED: real cashback programs have no sandbox API, so Penny applies a rate
    to sandbox payments instead of contacting any cashback provider.
    """
    now = now or utcnow()
    if session.scalar(select(CashbackCredit).where(CashbackCredit.paypal_event_id == event_key)):
        return None
    capture = usd(capture_amount)
    rate = settings.cashback_rate if rate is None else rate
    cashback = usd(capture * rate)
    credit = CashbackCredit(paypal_event_id=event_key, capture_amount=capture, cashback_amount=cashback, created_at=now)
    try:
        with session.begin_nested():
            session.add(credit)
            session.flush()
    except IntegrityError:
        return None
    if cashback > 0:
        ledger.credit_pot(session, cashback, "cashback", now)
    ledger.log_event(session, "cashback", explain.cashback_credited(cashback, capture, rate),
                     {"event": event_key, "capture": capture, "cashback": cashback}, now)
    return credit
