"""Fee-aware threshold and buy-only rebalancing: pure functions."""
from datetime import timedelta
from decimal import Decimal as D

from app.policy import PROFILES
from app.schemas import LedgerSnapshot, Quote, Rules
from app.threshold import decide, fee_drag_pct, min_invest_amount, plan_rebalance
from tests.conftest import NOW

GROWTH, RULES = PROFILES["growth"], Rules()


def snap(pot, age="0", values=None):
    return LedgerSnapshot(now=NOW, pot_available=D(pot), positions_value={k: D(v) for k, v in (values or {}).items()},
                          oldest_pending_age_days=D(age))


def test_fee_drag_math():
    assert fee_drag_pct(D("5.00"), D("0.50")) == D("0.1")                 # 10%
    assert round(fee_drag_pct(D("1.80"), D("0.50")), 3) == D("0.278")     # a $1.80 buy loses ~28%
    assert fee_drag_pct(D("0"), D("0.50")) == D("1")


def test_threshold_is_the_larger_of_min_trade_and_fee_over_drag():
    assert min_invest_amount(RULES) == D("10.00")                                   # 0.50 / 5%
    assert min_invest_amount(Rules(fee_assumed=D("0.10"))) == D("5.00")             # floored at MIN_TRADE
    assert min_invest_amount(Rules(max_fee_drag_pct=D("0"))) == D("5.00")


def test_below_threshold_holds():
    d = decide(snap("6.15", age="4"), GROWTH, RULES)
    assert not d.invest and d.code == "BELOW_THRESHOLD" and d.orders == []


def test_at_and_above_threshold_invests_the_whole_pot():
    d = decide(snap("12"), GROWTH, RULES)
    assert d.invest and d.code == "THRESHOLD_MET"
    assert {(o.ticker, o.dollar_amount) for o in d.orders} == {("VTI", D("9.60")), ("VXUS", D("2.40"))}
    assert sum(o.dollar_amount for o in d.orders) == D("12")
    assert decide(snap("10.00"), GROWTH, RULES).invest


def test_old_cash_forces_an_invest_despite_fee_drag():
    held = decide(snap("6", age="29.9"), GROWTH, RULES)
    forced = decide(snap("6", age="30"), GROWTH, RULES)
    assert not held.invest
    assert forced.invest and forced.code == "HOLD_MAX_DAYS_REACHED"
    assert sum(o.dollar_amount for o in forced.orders) == D("6")


def test_below_broker_minimum_never_invests_even_when_old():
    d = decide(snap("0.80", age="90"), GROWTH, RULES)
    assert not d.invest and d.code == "POT_BELOW_BROKER_MINIMUM"


def test_pot_too_small_to_respect_the_band_holds():
    d = decide(snap("2.50", age="40"), GROWTH, RULES)       # VXUS leg would be <$1 and fold into VTI -> 100% equities
    assert not d.invest and d.code == "POT_TOO_SMALL_FOR_BANDS"


def test_rebalance_sends_cash_to_the_most_underweight_fund():
    orders = plan_rebalance(snap("10", values={"VTI": "90"}), GROWTH, D("10"))
    assert [(o.ticker, o.dollar_amount) for o in orders] == [("VXUS", D("10.00"))]


def test_rebalance_never_sells_and_always_sums_to_the_pot():
    for pot in ("10.01", "33.33", "7.77", "100"):
        orders = plan_rebalance(snap(pot, values={"VTI": "40", "VXUS": "40"}), GROWTH, D(pot))
        assert all(o.side == "buy" and o.dollar_amount > 0 for o in orders)
        assert sum(o.dollar_amount for o in orders) == D(pot)


def test_orders_respect_the_broker_minimum_notional():
    for o in plan_rebalance(snap("5"), GROWTH, D("5")):
        assert o.dollar_amount >= D("1.00")
