"""Policy layer: fee-aware invest threshold and buy-only rebalancing plan (pure functions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.policy import Profile, band_check
from app.schemas import LedgerSnapshot, Proposal, Rules, usd

ZERO = Decimal("0")
BROKER_MIN_NOTIONAL = Decimal("1.00")     # smallest order the broker will take


@dataclass(frozen=True)
class ThresholdDecision:
    invest: bool
    code: str
    detail: str
    amount: Decimal                       # the pot we would invest
    threshold: Decimal                    # fee-aware minimum
    fee_drag: Decimal                     # fee / amount, as a fraction
    age_days: Decimal
    orders: list[Proposal] = field(default_factory=list)


def min_invest_amount(rules: Rules) -> Decimal:
    """Smallest pot that keeps assumed fees under MAX_FEE_DRAG_PCT, floored at MIN_TRADE_USD."""
    if rules.max_fee_drag_pct <= 0:
        return rules.min_trade
    return max(rules.min_trade, usd(rules.fee_assumed / rules.max_fee_drag_pct))


def fee_drag_pct(trade_amount: Decimal, fee: Decimal) -> Decimal:
    """Assumed fixed fee as a fraction of the trade (0.10 == 10%). A zero trade is 100% drag."""
    if trade_amount <= 0:
        return Decimal("1")
    return fee / trade_amount


def plan_rebalance(snapshot: LedgerSnapshot, profile: Profile, amount: Decimal) -> list[Proposal]:
    """Split `amount` across the profile's funds, favouring the most underweight. Buys only."""
    values = {t: snapshot.positions_value.get(t, ZERO) for t in profile.targets}
    other = sum((v for t, v in snapshot.positions_value.items() if t not in profile.targets), ZERO)
    total_after = sum(values.values(), ZERO) + other + amount
    shortfall = {t: max(profile.targets[t] * total_after - values[t], ZERO) for t in values}
    s = sum(shortfall.values(), ZERO)
    weights = {t: shortfall[t] / s for t in shortfall} if s > 0 else dict(profile.targets)

    alloc = {t: usd(amount * w) for t, w in weights.items()}
    largest = max(alloc, key=lambda t: alloc[t])
    alloc[largest] += amount - sum(alloc.values(), ZERO)          # park rounding dust on the biggest leg
    for t in list(alloc):                                          # fold legs below the broker minimum
        if t != largest and 0 < alloc[t] < BROKER_MIN_NOTIONAL:
            alloc[largest] += alloc[t]
            alloc[t] = ZERO
    return [Proposal(t, a, "buy") for t, a in sorted(alloc.items(), key=lambda kv: -kv[1]) if a > 0]


def decide(snapshot: LedgerSnapshot, profile: Profile, rules: Rules) -> ThresholdDecision:
    """Invest when pot >= max(MIN_TRADE, fee/MAX_DRAG), or when cash has waited HOLD_MAX_DAYS."""
    pot = snapshot.pot_available
    threshold = min_invest_amount(rules)
    drag = fee_drag_pct(pot, rules.fee_assumed)
    age = snapshot.oldest_pending_age_days

    def hold(code: str, detail: str) -> ThresholdDecision:
        return ThresholdDecision(False, code, detail, pot, threshold, drag, age)

    if pot < BROKER_MIN_NOTIONAL:
        return hold("POT_BELOW_BROKER_MINIMUM", f"Pot ${pot} is below the ${BROKER_MIN_NOTIONAL} broker minimum.")

    if pot >= threshold:
        code, detail = "THRESHOLD_MET", f"Pot ${pot} >= threshold ${threshold}."
    elif age >= rules.hold_max_days:
        code, detail = "HOLD_MAX_DAYS_REACHED", f"Cash has waited {age} days (limit {rules.hold_max_days})."
    else:
        return hold("BELOW_THRESHOLD", f"Pot ${pot} < threshold ${threshold}; fee drag would be {drag:.1%}.")

    orders = plan_rebalance(snapshot, profile, pot)
    ok, _, after = band_check(profile, snapshot.positions_value, orders)
    if not ok:
        return hold("POT_TOO_SMALL_FOR_BANDS",
                    "Pot is too small to split into orders that respect the allocation band.")
    return ThresholdDecision(True, code, detail, pot, threshold, drag, age, orders)
