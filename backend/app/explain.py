"""Explain layer: one-line human reasons rendered from ledger facts (never invented), plus a
grounding check that rejects LLM sentences containing numbers the tools never returned."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from app.schemas import Verdict, usd


def money(v: Any) -> str:
    return f"${usd(v):,.2f}"


def pct(v: Any) -> str:
    """Whole-number percent of a fraction: 0.76 -> '76%'."""
    return f"{(Decimal(str(v)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}%"


def pct1(v: Any) -> str:
    """Percent with at most one decimal: 0.1 -> '10%', 0.081 -> '8.1%'."""
    s = f"{(Decimal(str(v)) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}"
    return (s[:-2] if s.endswith(".0") else s) + "%"


def _band(low: Any, high: Any) -> str:
    return f"{int(Decimal(str(low)) * 100)}–{int(Decimal(str(high)) * 100)}%"


def _days(age: Any) -> str:
    n = int(Decimal(str(age)))
    return "under a day" if n < 1 else f"{n} day{'s' if n != 1 else ''}"


# ------------------------------------------------------------------ money in
def cashback_credited(cashback: Any, capture: Any, rate: Any) -> str:
    return f"Credited {money(cashback)} cashback from your {money(capture)} PayPal purchase ({pct1(rate)} — simulated)."


# ------------------------------------------------------------------ decisions
def held(pot: Any, age_days: Any, fee: Any, threshold: Any) -> str:
    drag = Decimal(str(fee)) / Decimal(str(pot)) if Decimal(str(pot)) > 0 else Decimal(1)
    return (f"Held {money(pot)} in your pot for {_days(age_days)}; fee drag on a {money(pot)} trade "
            f"would be {pct1(drag)}, so I waited (invest threshold {money(threshold)}).")


def forced_by_age(pot: Any, age_days: Any, limit: Any, drag: Any) -> str:
    return (f"Cash has waited {_days(age_days)} (limit {limit} days), so I'm investing {money(pot)} "
            f"even though fee drag is {pct1(drag)}.")


def needs_approval(ticker: str, amount: Any, cap: Any) -> str:
    return f"Order for {money(amount)} of {ticker} is above your {money(cap)} auto-approve cap — waiting for your approval."


def approved_waiting(ticker: str, amount: Any, seconds: int) -> str:
    return f"Approved {money(amount)} of {ticker}; it executes in {seconds}s unless you undo it."


def bought(ticker: str, amount: Any, w_before: Any, w_after: Any, target: Any, low: Any, high: Any) -> str:
    before = pct(w_before) if w_before is not None else "0%"
    return (f"Bought {money(amount)} of {ticker} to bring equities from {before} to {pct(w_after)} "
            f"(target {pct(target)}, band {_band(low, high)}).")


def undone(ticker: str, amount: Any) -> str:
    return f"Undone: the {money(amount)} {ticker} order was cancelled before it executed."


def rejected(verdict: Verdict) -> str:
    f = verdict.facts
    c = verdict.code
    if c == "BAND_VIOLATION":
        return (f"Rejected: order would push equities to {pct(f['weight_after'])}, outside your "
                f"{f['profile']} band of {_band(f['band_low'], f['band_high'])}.")
    if c == "TICKER_NOT_ALLOWED":
        return f"Rejected: {f['ticker']} isn't on your {f['profile']} allowlist ({', '.join(f['allowed'])})."
    if c == "INSUFFICIENT_POT":
        return f"Rejected: tried to use {money(f['amount'])} but only {money(f['pot'])} is available in your pot."
    if c == "QUOTE_MISSING":
        return f"Rejected: no price quote available for {f['ticker']}."
    if c == "QUOTE_STALE":
        return f"Rejected: the {f['ticker']} quote is {f['age']}s old (limit {f['limit']}s)."
    if c == "SELL_NOT_ALLOWED":
        return "Rejected: Penny only buys; selling needs an approved withdrawal."
    if c == "NON_POSITIVE_AMOUNT":
        return "Rejected: amounts must be greater than zero."
    return f"Rejected: {verdict.detail or c}."


def sold(ticker: str, proceeds: Any) -> str:
    return f"Sold {money(proceeds)} of {ticker} to raise cash for your withdrawal."


def execution_failed(ticker: str, amount: Any, reason: str) -> str:
    return f"Could not execute the {money(amount)} {ticker} order ({reason}); your cash is still in the pot."


# ------------------------------------------------------------------ money out
def payout_started(amount: Any) -> str:
    return f"Started a {money(amount)} withdrawal to your PayPal (sandbox payout); the money is in transit."


def payout_completed(amount: Any) -> str:
    return f"Payout of {money(amount)} completed (sandbox)."


def payout_failed(amount: Any, reason: str) -> str:
    return f"Payout of {money(amount)} failed ({reason}); the money is back in your pot."


def payout_suggested(amount: Any) -> str:
    return f"Suggested withdrawing {money(amount)}; only you can start a withdrawal (POST /withdraw)."


def agent_summary(invest: bool, proposals: int, total: Any, approved: int, pending: int, rejected_n: int) -> str:
    if not invest and proposals == 0:
        return "Reviewed your pot; nothing to invest right now."
    return (f"Proposed {proposals} order{'s' if proposals != 1 else ''} totalling {money(total)}: "
            f"{approved} approved, {pending} awaiting you, {rejected_n} rejected by policy.")


# ------------------------------------------------------------------ grounding
_NUM = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _numbers_in(obj: Any) -> Iterable[Decimal]:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float, Decimal)):
        yield Decimal(str(obj))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            try:
                yield Decimal(m.replace(",", ""))
            except InvalidOperation:
                continue
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers_in(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _numbers_in(v)


def is_grounded(text: str, facts: Iterable[Any]) -> bool:
    """True if every number in `text` appears (within rounding) in the tool results `facts`."""
    known: set[Decimal] = set()
    for n in _numbers_in(list(facts)):
        known.add(n)
        if 0 < n <= 1:
            known.add(n * 100)           # fractions are also quoted as percents
    for raw in _NUM.findall(text):
        t = Decimal(raw.replace(",", ""))
        places = -t.as_tuple().exponent if t.as_tuple().exponent < 0 else 0
        tol = Decimal("0.5") if places == 0 else Decimal("0.5") * (Decimal(10) ** -places)
        if not any(abs(t - k) <= tol for k in known):
            return False
    return True
