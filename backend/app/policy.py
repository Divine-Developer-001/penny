"""Policy layer: deterministic, pure validation of every proposed action. No I/O, no LLM, no clock."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from app.schemas import LedgerSnapshot, Proposal, Rules, Verdict, VerdictStatus

D = Decimal
ZERO = D("0")


@dataclass(frozen=True)
class Profile:
    """A risk profile. `core_ticker` is the 'equities' fund whose portfolio weight is banded."""
    name: str
    targets: dict[str, Decimal]
    core_ticker: str
    band_low: Decimal
    band_high: Decimal

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(self.targets)


PROFILES: dict[str, Profile] = {
    "conservative": Profile("conservative", {"VTI": D("0.30"), "BND": D("0.70")}, "VTI", D("0.20"), D("0.40")),
    "balanced": Profile("balanced", {"VTI": D("0.60"), "BND": D("0.40")}, "VTI", D("0.50"), D("0.70")),
    "growth": Profile("growth", {"VTI": D("0.80"), "VXUS": D("0.20")}, "VTI", D("0.70"), D("0.90")),
}


TICKER_NAMES = {"VTI": "US total stock market", "VXUS": "International stocks", "BND": "US total bond market"}


def _approved(code: str = "OK", detail: str = "", **facts) -> Verdict:
    return Verdict(VerdictStatus.APPROVED, code, detail, facts)


def _needs(code: str, detail: str, **facts) -> Verdict:
    return Verdict(VerdictStatus.NEEDS_APPROVAL, code, detail, facts)


def _rejected(code: str, detail: str, **facts) -> Verdict:
    return Verdict(VerdictStatus.REJECTED, code, detail, facts)


# ------------------------------------------------------------------ allocation maths
def core_weight(profile: Profile, values: Mapping[str, Decimal]) -> Decimal | None:
    """Weight of the core ('equities') fund in a {ticker: market value} map; None if empty."""
    total = sum(values.values(), ZERO)
    if total <= 0:
        return None
    return values.get(profile.core_ticker, ZERO) / total


def values_after(values: Mapping[str, Decimal], plan: Sequence[Proposal]) -> dict[str, Decimal]:
    out = dict(values)
    for leg in plan:
        if leg.side == "buy":
            out[leg.ticker] = out.get(leg.ticker, ZERO) + leg.dollar_amount
    return out


def band_check(profile: Profile, values: Mapping[str, Decimal], plan: Sequence[Proposal]
               ) -> tuple[bool, Decimal | None, Decimal | None]:
    """Return (ok, weight_before, weight_after) for executing `plan` on `values`.

    ok when the post-trade core weight is inside the profile band. Exception: if the portfolio is
    already out of band (market drift), a trade that moves the weight strictly closer to target is
    allowed, otherwise every corrective buy would be blocked.
    """
    before = core_weight(profile, values)
    after = core_weight(profile, values_after(values, plan))
    if after is None:
        return True, before, after
    if profile.band_low <= after <= profile.band_high:
        return True, before, after
    target = profile.targets[profile.core_ticker]
    improves = before is not None and abs(after - target) < abs(before - target)
    return improves, before, after


# ------------------------------------------------------------------ validation
def validate(proposal: Proposal, profile: Profile, snapshot: LedgerSnapshot,
             rules: Rules | None = None, plan: Sequence[Proposal] | None = None) -> Verdict:
    """Validate one proposed order. `plan` lists sibling legs executed together (incl. this one)."""
    rules = rules or Rules()
    ticker = proposal.ticker.upper()
    amount = proposal.dollar_amount
    legs = list(plan) if plan else [proposal]
    if proposal not in legs:
        legs.append(proposal)

    if amount <= 0:
        return _rejected("NON_POSITIVE_AMOUNT", "Order amount must be positive.", amount=amount)
    if proposal.side not in ("buy", "sell"):
        return _rejected("INVALID_SIDE", "Side must be buy or sell.", side=proposal.side)
    if ticker not in profile.allowlist:                                           # rule 1
        return _rejected("TICKER_NOT_ALLOWED", f"{ticker} is not on the {profile.name} allowlist.",
                         ticker=ticker, profile=profile.name, allowed=sorted(profile.allowlist))
    if proposal.side == "sell":                                                   # rule 5
        if not proposal.linked_withdrawal:
            return _rejected("SELL_NOT_ALLOWED", "Penny only buys; sells need an approved withdrawal.")
        return _needs("SELL_NEEDS_WITHDRAWAL_APPROVAL", "Sells are only allowed for an approved withdrawal.")

    quote = snapshot.quotes.get(ticker)                                           # rule 6
    if quote is None or quote.price <= 0:
        return _rejected("QUOTE_MISSING", f"No usable quote for {ticker}.", ticker=ticker)
    age = int((snapshot.now - quote.as_of).total_seconds())
    if age > rules.quote_staleness_seconds:
        return _rejected("QUOTE_STALE", f"{ticker} quote is {age}s old.", ticker=ticker, age=age,
                         limit=rules.quote_staleness_seconds)

    plan_total = sum((leg.dollar_amount for leg in legs if leg.side == "buy"), ZERO)  # rule 4
    if amount > snapshot.pot_available or plan_total > snapshot.pot_available:
        return _rejected("INSUFFICIENT_POT", "Order exceeds the available pot.",
                         amount=max(amount, plan_total), pot=snapshot.pot_available)

    ok, before, after = band_check(profile, snapshot.positions_value, legs)       # rule 2
    if not ok:
        return _rejected("BAND_VIOLATION", "Allocation would leave the profile band.",
                         weight_before=before, weight_after=after, band_low=profile.band_low,
                         band_high=profile.band_high, profile=profile.name)

    if amount > rules.trade_cap:                                                  # rule 3
        return _needs("OVER_TRADE_CAP", f"Order is above the ${rules.trade_cap} auto-approve cap.",
                      ticker=ticker, amount=amount, cap=rules.trade_cap)
    return _approved("OK", "Within allowlist, band, cap and pot.", weight_before=before, weight_after=after)


def validate_payout(amount: Decimal, snapshot: LedgerSnapshot, *, initiated_by_user: bool) -> Verdict:
    """Validate a withdrawal. Money only leaves when the *user* asks; the agent can only suggest."""
    if amount <= 0:
        return _rejected("NON_POSITIVE_AMOUNT", "Payout amount must be positive.", amount=amount)
    if amount > snapshot.pot_available:
        return _rejected("INSUFFICIENT_POT", "Payout exceeds the available pot.",
                         amount=amount, pot=snapshot.pot_available)
    if not initiated_by_user:
        return _needs("PAYOUT_REQUIRES_USER", "Only the user can start a withdrawal.", amount=amount)
    return _approved("OK", "Payout within the available pot.")


def validate_withdrawal_sell(ticker: str, amount: Decimal, snapshot: LedgerSnapshot,
                             rules: Rules | None = None) -> Verdict:
    """Check one sell that raises cash for a user-initiated withdrawal (the user's request is the approval)."""
    rules = rules or Rules()
    ticker = ticker.upper()
    if amount <= 0:
        return _rejected("NON_POSITIVE_AMOUNT", "Sell amount must be positive.", amount=amount)
    quote = snapshot.quotes.get(ticker)
    if quote is None or quote.price <= 0:
        return _rejected("QUOTE_MISSING", f"No usable quote for {ticker}.", ticker=ticker)
    age = int((snapshot.now - quote.as_of).total_seconds())
    if age > rules.quote_staleness_seconds:
        return _rejected("QUOTE_STALE", f"{ticker} quote is {age}s old.", ticker=ticker, age=age,
                         limit=rules.quote_staleness_seconds)
    return _approved("OK", "Sell raises cash for a user-initiated withdrawal.")
