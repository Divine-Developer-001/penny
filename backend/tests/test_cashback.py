"""Cashback rule: exact Decimal maths and idempotency by event id."""
from decimal import Decimal as D

from sqlalchemy import select

from app import cashback, ledger
from app.models import CashbackCredit, LedgerEvent
from tests.conftest import make_settings


def test_two_percent_math_is_exact_decimal(session):
    s = make_settings()
    credit = cashback.apply(session, s, "evt-1", "21.00")
    assert credit.cashback_amount == D("0.42") and isinstance(credit.cashback_amount, D)
    assert ledger.available_balance(session) == D("0.42")


def test_rounding_is_half_up_to_the_cent(session):
    s = make_settings()
    assert cashback.apply(session, s, "a", "0.25").cashback_amount == D("0.01")     # 0.005 -> 0.01
    assert cashback.apply(session, s, "b", "19.99").cashback_amount == D("0.40")     # 0.3998 -> 0.40
    assert cashback.apply(session, s, "c", "0.10").cashback_amount == D("0.00")      # 0.002 -> 0.00


def test_zero_cashback_credits_nothing_to_the_pot(session):
    cashback.apply(session, make_settings(), "tiny", "0.10")
    assert ledger.available_balance(session) == D("0")


def test_idempotent_by_event_id(session):
    s = make_settings()
    first = cashback.apply(session, s, "evt-1", "21.00")
    again = cashback.apply(session, s, "evt-1", "21.00")
    assert first is not None and again is None
    assert ledger.available_balance(session) == D("0.42")
    assert len(session.scalars(select(CashbackCredit)).all()) == 1


def test_custom_rate_from_settings(session):
    credit = cashback.apply(session, make_settings(cashback_rate=D("0.05")), "evt-5", "10.00")
    assert credit.cashback_amount == D("0.50")


def test_writes_one_explained_ledger_event(session):
    cashback.apply(session, make_settings(), "evt-1", "21.00")
    events = session.scalars(select(LedgerEvent)).all()
    assert len(events) == 1
    assert events[0].description == "Credited $0.42 cashback from your $21.00 PayPal purchase (2% — simulated)."
