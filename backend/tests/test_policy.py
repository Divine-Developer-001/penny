"""Policy engine: pure, deterministic, no network. Every hard rule has a test."""
from datetime import timedelta
from decimal import Decimal as D

from app.policy import PROFILES, band_check, validate, validate_payout
from app.schemas import LedgerSnapshot, Proposal, Quote, Rules, VerdictStatus
from tests.conftest import NOW

GROWTH = PROFILES["growth"]
RULES = Rules()


def snap(pot="100", values=None, quote_age=0, tickers=("VTI", "VXUS", "BND")):
    quotes = {t: Quote(t, D("100"), NOW - timedelta(seconds=quote_age)) for t in tickers}
    return LedgerSnapshot(now=NOW, pot_available=D(pot), positions_value={k: D(v) for k, v in (values or {}).items()},
                          quotes=quotes)


def buy(ticker, amount):
    return Proposal(ticker, D(amount))


def test_allowlist_violation_individual_stock():
    v = validate(buy("TSLA", "5"), GROWTH, snap(tickers=("TSLA",)), RULES)
    assert v.status is VerdictStatus.REJECTED and v.code == "TICKER_NOT_ALLOWED"


def test_allowlist_is_per_profile():
    assert validate(buy("BND", "5"), GROWTH, snap(), RULES).code == "TICKER_NOT_ALLOWED"
    assert validate(buy("BND", "5"), PROFILES["conservative"], snap(values={"VTI": "30", "BND": "70"}), RULES).status \
        is VerdictStatus.APPROVED


def test_band_violation_pushing_equities_to_92_percent():
    v = validate(buy("VTI", "100"), GROWTH, snap(pot="200", values={"VTI": "85", "VXUS": "15"}), RULES)
    assert v.status is VerdictStatus.REJECTED and v.code == "BAND_VIOLATION"
    assert v.facts["weight_after"] == D("185") / D("200")


def test_over_cap_needs_approval():
    v = validate(buy("VTI", "30"), GROWTH, snap(values={"VTI": "80", "VXUS": "20"}), RULES)
    assert v.status is VerdictStatus.NEEDS_APPROVAL and v.code == "OVER_TRADE_CAP"


def test_exactly_at_cap_is_auto_approved():
    v = validate(buy("VTI", "25"), GROWTH, snap(values={"VTI": "80", "VXUS": "20"}), RULES)
    assert v.status is VerdictStatus.APPROVED


def test_over_pot_rejected():
    v = validate(buy("VTI", "12"), GROWTH, snap(pot="10", values={"VTI": "80", "VXUS": "20"}), RULES)
    assert v.status is VerdictStatus.REJECTED and v.code == "INSUFFICIENT_POT"


def test_non_positive_amounts_rejected():
    assert validate(buy("VTI", "0"), GROWTH, snap(), RULES).code == "NON_POSITIVE_AMOUNT"
    assert validate(buy("VTI", "-5"), GROWTH, snap(), RULES).code == "NON_POSITIVE_AMOUNT"


def test_sell_rejected_unless_linked_to_withdrawal():
    assert validate(Proposal("VTI", D("5"), "sell"), GROWTH, snap(), RULES).code == "SELL_NOT_ALLOWED"
    linked = validate(Proposal("VTI", D("5"), "sell", linked_withdrawal=True), GROWTH, snap(), RULES)
    assert linked.status is VerdictStatus.NEEDS_APPROVAL


def test_valid_buy_approved():
    v = validate(buy("VTI", "8"), GROWTH, snap(pot="20", values={"VTI": "80", "VXUS": "20"}), RULES)
    assert v.status is VerdictStatus.APPROVED and v.code == "OK"


def test_stale_quote_rejected():
    ok = validate(buy("VTI", "8"), GROWTH, snap(values={"VTI": "80", "VXUS": "20"}, quote_age=300), RULES)
    stale = validate(buy("VTI", "8"), GROWTH, snap(values={"VTI": "80", "VXUS": "20"}, quote_age=301), RULES)
    assert ok.status is VerdictStatus.APPROVED
    assert stale.status is VerdictStatus.REJECTED and stale.code == "QUOTE_STALE"


def test_missing_quote_rejected():
    s = LedgerSnapshot(now=NOW, pot_available=D("100"))
    assert validate(buy("VTI", "5"), GROWTH, s, RULES).code == "QUOTE_MISSING"


def test_first_buy_needs_the_whole_plan_to_satisfy_the_band():
    plan = [buy("VTI", "8"), buy("VXUS", "2")]
    alone = validate(buy("VTI", "8"), GROWTH, snap(pot="10"), RULES)
    together = validate(plan[0], GROWTH, snap(pot="10"), RULES, plan=plan)
    assert alone.code == "BAND_VIOLATION"                     # 100% equities on its own
    assert together.status is VerdictStatus.APPROVED          # 80/20 as a plan


def test_plan_total_cannot_exceed_pot():
    plan = [buy("VTI", "8"), buy("VXUS", "5")]
    assert validate(plan[0], GROWTH, snap(pot="10"), RULES, plan=plan).code == "INSUFFICIENT_POT"


def test_drifted_portfolio_may_make_corrective_buys():
    drifted = {"VTI": "98", "VXUS": "2"}                        # 98% equities: outside the 70-90 band
    corrective = validate(buy("VXUS", "5"), GROWTH, snap(values=drifted), RULES)
    worsening = validate(buy("VTI", "5"), GROWTH, snap(values=drifted), RULES)
    assert corrective.status is VerdictStatus.APPROVED           # 93.3% is still out of band, but closer
    assert worsening.code == "BAND_VIOLATION"


def test_band_check_is_pure():
    values = {"VTI": D("80"), "VXUS": D("20")}
    before = dict(values)
    assert band_check(GROWTH, values, [buy("VXUS", "5")]) == band_check(GROWTH, values, [buy("VXUS", "5")])
    assert values == before


def test_profiles_match_the_documented_table():
    assert PROFILES["conservative"].targets == {"VTI": D("0.30"), "BND": D("0.70")}
    assert (PROFILES["conservative"].band_low, PROFILES["conservative"].band_high) == (D("0.20"), D("0.40"))
    assert PROFILES["balanced"].targets == {"VTI": D("0.60"), "BND": D("0.40")}
    assert (PROFILES["balanced"].band_low, PROFILES["balanced"].band_high) == (D("0.50"), D("0.70"))
    assert PROFILES["growth"].targets == {"VTI": D("0.80"), "VXUS": D("0.20")}
    assert (PROFILES["growth"].band_low, PROFILES["growth"].band_high) == (D("0.70"), D("0.90"))


def test_payout_rules():
    s = snap(pot="10")
    assert validate_payout(D("5"), s, initiated_by_user=True).status is VerdictStatus.APPROVED
    assert validate_payout(D("11"), s, initiated_by_user=True).code == "INSUFFICIENT_POT"
    assert validate_payout(D("0"), s, initiated_by_user=True).code == "NON_POSITIVE_AMOUNT"
    assert validate_payout(D("5"), s, initiated_by_user=False).status is VerdictStatus.NEEDS_APPROVAL
