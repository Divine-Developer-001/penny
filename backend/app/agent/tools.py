"""Agent layer: tool schemas and dispatch. Tools are the ONLY way the agent touches the ledger, and
`propose_order` always runs `policy.validate` first."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy.orm import Session

from app import explain, ledger, policy, prices, threshold
from app.brokerage.base import Broker
from app.config import Settings
from app.models import ProposalStatus, TradeProposal
from app.schemas import Proposal, Rules, VerdictStatus, usd

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    session: Session
    settings: Settings
    rules: Rules
    broker: Broker
    profile: policy.Profile
    now: datetime
    plan: list[Proposal] = field(default_factory=list)          # legs of the current threshold plan
    facts: list[Any] = field(default_factory=list)             # every tool result, for grounding
    decision: threshold.ThresholdDecision | None = None
    proposal_ids: list[int] = field(default_factory=list)
    proposal_results: list[dict[str, Any]] = field(default_factory=list)
    explanation: str | None = None


def _fn(name: str, description: str, properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}}


TOOL_SCHEMAS: list[dict] = [
    _fn("get_pending_balance", "Cash in the pot: available, committed to open proposals, and uncommitted, plus the age of the oldest cash."),
    _fn("get_portfolio", "Current paper positions, market values, weights and the profile's targets and band."),
    _fn("get_quotes", "Latest price quotes for the given tickers.",
        {"tickers": {"type": "array", "items": {"type": "string"}}}, ("tickers",)),
    _fn("get_threshold_decision", "Deterministic fee-aware decision: invest now or hold, with the exact orders to propose."),
    _fn("propose_order", "Propose a buy order. The policy engine validates it; you cannot bypass it.",
        {"ticker": {"type": "string"}, "dollar_amount": {"type": "number"},
         "rationale": {"type": "string", "description": "One sentence."}}, ("ticker", "dollar_amount", "rationale")),
    _fn("propose_payout", "Suggest a withdrawal. Always needs the user to start it; this only records the suggestion.",
        {"amount": {"type": "number"}, "rationale": {"type": "string"}}, ("amount", "rationale")),
    _fn("explain", "Record the final one-sentence explanation for the user, using only numbers the tools returned.",
        {"text": {"type": "string"}}, ("text",)),
]


def _quotes(ctx: ToolContext):
    held = [p.ticker for p in ledger.get_positions(ctx.session)]
    return prices.get_quotes(ctx.broker, list(ctx.profile.targets) + held)


def _snapshot(ctx: ToolContext):
    return ledger.build_snapshot(ctx.session, _quotes(ctx), ctx.now)


def _pct_str(v: Decimal) -> str:
    return f"{(v * 100).quantize(Decimal('0.1'))}"


# ------------------------------------------------------------------ handlers
def get_pending_balance(ctx: ToolContext) -> dict[str, Any]:
    available, committed = ledger.available_balance(ctx.session), ledger.committed_amount(ctx.session)
    return {"available_usd": available, "committed_usd": committed, "uncommitted_usd": ledger.uncommitted_pot(ctx.session),
            "oldest_cash_age_days": ledger.oldest_pending_age_days(ctx.session, ctx.now)}


def get_portfolio(ctx: ToolContext) -> dict[str, Any]:
    snap = _snapshot(ctx)
    total = sum(snap.positions_value.values(), Decimal(0))
    weights = {t: (v / total if total > 0 else Decimal(0)) for t, v in snap.positions_value.items()}
    core = policy.core_weight(ctx.profile, snap.positions_value)
    return {
        "profile": ctx.profile.name,
        "targets_percent": {t: _pct_str(w) for t, w in ctx.profile.targets.items()},
        "equities_ticker": ctx.profile.core_ticker,
        "equities_band_percent": [_pct_str(ctx.profile.band_low), _pct_str(ctx.profile.band_high)],
        "values_usd": {t: usd(v) for t, v in snap.positions_value.items()},
        "weights_percent": {t: _pct_str(w) for t, w in weights.items()},
        "equities_weight_percent": _pct_str(core) if core is not None else "0.0",
        "total_value_usd": usd(total),
    }


def get_quotes(ctx: ToolContext, tickers: list[str]) -> dict[str, Any]:
    q = prices.get_quotes(ctx.broker, tickers)
    return {"quotes": {t: {"price": v.price, "age_seconds": int((ctx.now - v.as_of).total_seconds())} for t, v in q.items()},
            "missing": sorted({t.upper() for t in tickers} - set(q))}


def get_threshold_decision(ctx: ToolContext) -> dict[str, Any]:
    decision = threshold.decide(_snapshot(ctx), ctx.profile, ctx.rules)
    ctx.decision, ctx.plan = decision, list(decision.orders)
    return {"invest": decision.invest, "code": decision.code, "detail": decision.detail,
            "pot_usd": decision.amount, "threshold_usd": decision.threshold, "fee_assumed_usd": ctx.rules.fee_assumed,
            "fee_drag_percent": _pct_str(decision.fee_drag), "oldest_cash_age_days": decision.age_days,
            "orders": [{"ticker": o.ticker, "dollar_amount": o.dollar_amount} for o in decision.orders]}


def propose_order(ctx: ToolContext, ticker: str, dollar_amount: float | str, rationale: str = "") -> dict[str, Any]:
    ticker = str(ticker).upper().strip()
    try:
        amount = usd(dollar_amount)
    except (InvalidOperation, ValueError):
        return {"error": f"invalid dollar_amount {dollar_amount!r}"}
    leg = Proposal(ticker, amount, "buy", False, rationale)
    in_plan = any(p.ticker == ticker for p in ctx.plan)
    plan = [leg if p.ticker == ticker else p for p in ctx.plan] if in_plan else [leg]
    verdict = policy.validate(leg, ctx.profile, _snapshot(ctx), ctx.rules, plan=plan)       # THE GATE

    row = TradeProposal(ticker=ticker, dollar_amount=amount, side="buy", policy_reason=verdict.code,
                        llm_rationale=(rationale or "")[:400], created_at=ctx.now)
    if verdict.status is VerdictStatus.APPROVED:
        row.status = ProposalStatus.APPROVED
        row.undo_deadline = ctx.now + timedelta(seconds=ctx.rules.undo_window_seconds)
        text = explain.approved_waiting(ticker, amount, ctx.rules.undo_window_seconds)
    elif verdict.status is VerdictStatus.NEEDS_APPROVAL:
        row.status = ProposalStatus.PENDING_APPROVAL
        text = explain.needs_approval(ticker, amount, ctx.rules.trade_cap)
    else:
        row.status = ProposalStatus.REJECTED
        text = explain.rejected(verdict)
    ctx.session.add(row)
    ctx.session.flush()
    ledger.log_event(ctx.session, "proposal", text, {"proposal_id": row.id, "verdict": verdict.status.value,
                                                      "code": verdict.code}, ctx.now)
    ctx.plan = [p for p in ctx.plan if p.ticker != ticker]
    ctx.proposal_ids.append(row.id)
    # Execution happens after the undo window (or at the end of the run when the window is 0), see run_agent.
    result = {"proposal_id": row.id, "verdict": verdict.status.value, "code": verdict.code, "detail": verdict.detail,
              "status": row.status, "undo_window_seconds": ctx.rules.undo_window_seconds}
    ctx.proposal_results.append(result)
    return result


def propose_payout(ctx: ToolContext, amount: float | str, rationale: str = "") -> dict[str, Any]:
    try:
        amt = usd(amount)
    except (InvalidOperation, ValueError):
        return {"error": f"invalid amount {amount!r}"}
    verdict = policy.validate_payout(amt, _snapshot(ctx), initiated_by_user=False)
    if verdict.status is not VerdictStatus.REJECTED:
        ledger.log_event(ctx.session, "payout_suggested", explain.payout_suggested(amt), {"rationale": rationale}, ctx.now)
    return {"verdict": verdict.status.value, "code": verdict.code, "detail": verdict.detail}


def explain_tool(ctx: ToolContext, text: str) -> dict[str, Any]:
    """Store the agent's sentence only if every number in it came from a tool; else use a template."""
    text = (text or "").strip()
    grounded = bool(text) and explain.is_grounded(text, ctx.facts)
    if not grounded:
        if ctx.decision is not None and not ctx.decision.invest and not ctx.proposal_results:
            d = ctx.decision
            text = explain.held(d.amount, d.age_days, ctx.rules.fee_assumed, d.threshold) \
                if d.code == "BELOW_THRESHOLD" else d.detail
        else:
            res = ctx.proposal_results
            text = explain.agent_summary(
                bool(ctx.decision and ctx.decision.invest), len(res), _total(ctx),
                sum(r["verdict"] == "APPROVED" for r in res), sum(r["verdict"] == "NEEDS_APPROVAL" for r in res),
                sum(r["verdict"] == "REJECTED" for r in res))
    ctx.explanation = text
    ledger.log_event(ctx.session, "agent_summary", text, {"grounded": grounded}, ctx.now)
    return {"recorded": text, "grounded": grounded}


def _total(ctx: ToolContext) -> Decimal:
    rows = [ctx.session.get(TradeProposal, r["proposal_id"]) for r in ctx.proposal_results]
    return sum((r.dollar_amount for r in rows if r and r.status != ProposalStatus.REJECTED), Decimal(0))


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_pending_balance": get_pending_balance, "get_portfolio": get_portfolio, "get_quotes": get_quotes,
    "get_threshold_decision": get_threshold_decision, "propose_order": propose_order,
    "propose_payout": propose_payout, "explain": explain_tool,
}


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Run one tool call. Errors become tool results so the model can see them; facts are recorded."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        result = handler(ctx, **(args or {}))
    except TypeError as exc:
        result = {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:                                  # noqa: BLE001
        logger.exception("tool %s failed", name)
        result = {"error": f"{name} failed: {exc}"}
    if name != "explain":
        ctx.facts.append(result)
    return json.loads(json.dumps(result, default=str))
