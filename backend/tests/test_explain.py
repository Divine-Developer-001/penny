"""Every explanation template renders with real numbers; the grounding check catches invented ones."""
from decimal import Decimal as D

from app import explain
from app.schemas import Verdict, VerdictStatus


def rej(code, **facts):
    return Verdict(VerdictStatus.REJECTED, code, "", facts)


def test_cashback_template():
    assert explain.cashback_credited(D("0.42"), D("21.00"), D("0.02")) == \
        "Credited $0.42 cashback from your $21.00 PayPal purchase (2% — simulated)."


def test_held_template_uses_real_pot_age_and_drag():
    text = explain.held(D("6.15"), D("4.2"), D("0.50"), D("10.00"))
    assert text == ("Held $6.15 in your pot for 4 days; fee drag on a $6.15 trade would be 8.1%, "
                    "so I waited (invest threshold $10.00).")
    assert "under a day" in explain.held(D("6.15"), D("0.3"), D("0.50"), D("10.00"))
    assert "1 day;" in explain.held(D("6.15"), D("1"), D("0.50"), D("10.00"))


def test_bought_template():
    assert explain.bought("VTI", D("12"), D("0.72"), D("0.76"), D("0.80"), D("0.70"), D("0.90")) == \
        "Bought $12.00 of VTI to bring equities from 72% to 76% (target 80%, band 70–90%)."
    assert "from 0% to" in explain.bought("VTI", D("8"), None, D("0.8"), D("0.8"), D("0.7"), D("0.9"))


def test_rejection_templates():
    band = explain.rejected(rej("BAND_VIOLATION", weight_after=D("0.95"), band_low=D("0.70"), band_high=D("0.90"), profile="growth"))
    assert band == "Rejected: order would push equities to 95%, outside your growth band of 70–90%."
    assert "TSLA" in explain.rejected(rej("TICKER_NOT_ALLOWED", ticker="TSLA", profile="growth", allowed=["VTI", "VXUS"]))
    assert explain.rejected(rej("INSUFFICIENT_POT", amount=D("12"), pot=D("10"))) == \
        "Rejected: tried to use $12.00 but only $10.00 is available in your pot."
    assert "300s" in explain.rejected(rej("QUOTE_STALE", ticker="VTI", age=420, limit=300))
    assert "VTI" in explain.rejected(rej("QUOTE_MISSING", ticker="VTI"))
    assert "only buys" in explain.rejected(rej("SELL_NOT_ALLOWED"))


def test_approval_and_undo_templates():
    assert "$30.00" in explain.needs_approval("VTI", D("30"), D("25")) and "$25.00" in explain.needs_approval("VTI", D("30"), D("25"))
    assert "60s" in explain.approved_waiting("VTI", D("8"), 60)
    assert "cancelled" in explain.undone("VTI", D("8"))


def test_forced_by_age_template():
    text = explain.forced_by_age(D("6"), D("30"), 30, D("0.0833"))
    assert "30 days" in text and "$6.00" in text and "8.3%" in text


def test_payout_templates():
    assert "$5.00" in explain.payout_started(D("5")) and "in transit" in explain.payout_started(D("5"))
    assert explain.payout_completed(D("5")) == "Payout of $5.00 completed (sandbox)."
    assert "back in your pot" in explain.payout_failed(D("5"), "SENDER_EMAIL_UNCONFIRMED")


def test_agent_summary_template():
    assert explain.agent_summary(False, 0, D("0"), 0, 0, 0) == "Reviewed your pot; nothing to invest right now."
    assert explain.agent_summary(True, 2, D("12"), 1, 1, 0) == \
        "Proposed 2 orders totalling $12.00: 1 approved, 1 awaiting you, 0 rejected by policy."


def test_grounding_accepts_numbers_from_tools_and_percent_forms():
    facts = [{"pot_usd": D("12.00"), "fee_drag_percent": "4.2", "orders": [{"ticker": "VTI", "dollar_amount": D("9.60")}]},
             {"fee_fraction": D("0.05")}]
    assert explain.is_grounded("Invested $12.00, including $9.60 in VTI.", facts)
    assert explain.is_grounded("Fee drag would be 4.2%.", facts)
    assert explain.is_grounded("A 5% fee cap applies.", facts)           # 0.05 also counts as 5
    assert explain.is_grounded("Nothing to do.", facts)                   # no numbers at all


def test_grounding_rejects_invented_numbers():
    facts = [{"pot_usd": D("12.00")}]
    assert not explain.is_grounded("You will earn 37% a year.", facts)
    assert not explain.is_grounded("Invested $15.00.", facts)
