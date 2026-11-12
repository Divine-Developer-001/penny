"""Money-in / ledger layer: every pot, position, proposal-execution and event write lives here."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import explain, policy, prices
from app.brokerage.base import Broker
from app.config import Settings
from app.models import (CashbackCredit, LedgerEvent, PendingCash, Payout, PayoutStatus, Position, ProposalStatus, Setting,
                        TradeProposal, PortfolioSnapshot)
from app.schemas import LedgerSnapshot, Quote, units, usd, utcnow
from app.threshold import BROKER_MIN_NOTIONAL

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
OPEN_STATUSES = (ProposalStatus.APPROVED, ProposalStatus.PENDING_APPROVAL)


class InsufficientFunds(Exception):
    pass


class SellRejected(Exception):
    """The policy engine refused a sell (for example a stale quote while the market is closed)."""


# ------------------------------------------------------------------ events & settings
def log_event(session: Session, kind: str, description: str, payload: Mapping[str, Any] | None = None,
              now: datetime | None = None) -> LedgerEvent:
    """Append one human-readable line to the append-only event log."""
    clean = json.loads(json.dumps(dict(payload or {}), default=str))
    ev = LedgerEvent(kind=kind, description=description, payload=clean, created_at=now or utcnow())
    session.add(ev)
    session.flush()
    return ev


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.flush()


def get_profile(session: Session, settings: Settings) -> policy.Profile:
    name = get_setting(session, "RISK_PROFILE") or settings.risk_profile
    return policy.PROFILES.get(name, policy.PROFILES["growth"])


# ------------------------------------------------------------------ the pot
def _rows(session: Session, status: str) -> list[PendingCash]:
    return list(session.scalars(select(PendingCash).where(PendingCash.status == status)
                                .order_by(PendingCash.created_at, PendingCash.id)))


def available_balance(session: Session) -> Decimal:
    return sum((r.amount for r in _rows(session, "available")), ZERO)


def in_transit_balance(session: Session) -> Decimal:
    return sum((r.amount for r in _rows(session, "in_transit")), ZERO)


def committed_proposals(session: Session) -> list[TradeProposal]:
    return list(session.scalars(select(TradeProposal).where(TradeProposal.status.in_(OPEN_STATUSES))
                                .order_by(TradeProposal.id)))


def committed_amount(session: Session) -> Decimal:
    return sum((p.dollar_amount for p in committed_proposals(session) if p.side == "buy"), ZERO)


def uncommitted_pot(session: Session) -> Decimal:
    return max(available_balance(session) - committed_amount(session), ZERO)


def credit_pot(session: Session, amount: Decimal, source: str = "cashback", now: datetime | None = None) -> PendingCash:
    row = PendingCash(amount=usd(amount), status="available", source=source, created_at=now or utcnow())
    session.add(row)
    session.flush()
    return row


def consume_pot(session: Session, amount: Decimal) -> None:
    """Take `amount` from the available pot, oldest cash first."""
    amount = usd(amount)
    if amount > available_balance(session):
        raise InsufficientFunds(f"need {amount}, have {available_balance(session)}")
    remaining = amount
    for row in _rows(session, "available"):
        if remaining <= 0:
            break
        take = min(row.amount, remaining)
        row.amount -= take
        remaining -= take
        if row.amount <= 0:
            session.delete(row)
    session.flush()


def move_to_in_transit(session: Session, amount: Decimal) -> PendingCash:
    consume_pot(session, amount)
    row = PendingCash(amount=usd(amount), status="in_transit", source="withdrawal", created_at=utcnow())
    session.add(row)
    session.flush()
    return row


def _release_in_transit(session: Session, payout: Payout, restore: bool) -> None:
    row = session.get(PendingCash, payout.pending_cash_id) if payout.pending_cash_id else None
    if row is not None and row.status == "in_transit":
        if restore:
            row.status, row.source = "available", "payout_restored"
        else:
            session.delete(row)
    elif restore:                                    # e.g. payout returned after it had completed
        credit_pot(session, payout.amount, "payout_restored")
    session.flush()


def complete_payout(session: Session, payout: Payout) -> None:
    if payout.status in (PayoutStatus.COMPLETED, PayoutStatus.FAILED):
        return
    payout.status = PayoutStatus.COMPLETED
    _release_in_transit(session, payout, restore=False)
    log_event(session, "payout_completed", explain.payout_completed(payout.amount), {"payout_id": payout.id})


def fail_payout(session: Session, payout: Payout, reason: str) -> None:
    if payout.status == PayoutStatus.FAILED:
        return
    payout.status, payout.detail = PayoutStatus.FAILED, reason[:500]
    _release_in_transit(session, payout, restore=True)
    log_event(session, "payout_failed", explain.payout_failed(payout.amount, reason), {"payout_id": payout.id})


# ------------------------------------------------------------------ positions & snapshots
def get_positions(session: Session) -> list[Position]:
    return list(session.scalars(select(Position).order_by(Position.ticker)))


def apply_fill(session: Session, ticker: str, quantity: Decimal, price: Decimal, now: datetime) -> Position:
    pos = session.get(Position, ticker)
    if pos is None:
        pos = Position(ticker=ticker, quantity=units(quantity), avg_cost=units(price), updated_at=now)
        session.add(pos)
    else:
        new_qty = pos.quantity + quantity
        pos.avg_cost = units((pos.quantity * pos.avg_cost + quantity * price) / new_qty)
        pos.quantity = units(new_qty)
        pos.updated_at = now
    session.flush()
    return pos


def oldest_pending_age_days(session: Session, now: datetime) -> Decimal:
    rows = _rows(session, "available")
    if not rows:
        return ZERO
    seconds = max((now - rows[0].created_at).total_seconds(), 0)
    return (Decimal(str(seconds)) / Decimal(86400)).quantize(Decimal("0.01"))


def build_snapshot(session: Session, quotes: Mapping[str, Quote], now: datetime) -> LedgerSnapshot:
    """Facts for the pure functions. positions_value is pro-forma: it counts committed proposals."""
    values: dict[str, Decimal] = {}
    for pos in get_positions(session):
        price = quotes[pos.ticker].price if pos.ticker in quotes else pos.avg_cost
        values[pos.ticker] = pos.quantity * price
    for prop in committed_proposals(session):
        if prop.side == "buy":
            values[prop.ticker] = values.get(prop.ticker, ZERO) + prop.dollar_amount
    return LedgerSnapshot(now=now, pot_available=uncommitted_pot(session), positions_value=values,
                          quotes=dict(quotes), oldest_pending_age_days=oldest_pending_age_days(session, now))


# ------------------------------------------------------------------ executing approved proposals
def _actual_values(session: Session, broker: Broker) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for pos in get_positions(session):
        try:
            price = broker.quote(pos.ticker).price
        except Exception:                                    # noqa: BLE001  fall back to cost basis
            price = pos.avg_cost
        out[pos.ticker] = pos.quantity * price
    return out


def _batch_values(session: Session, broker: Broker, proposal: TradeProposal) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Values after / before this proposal's whole batch (legs created together), so a two-order
    basket reads 'from 0% to 80%' instead of showing a misleading half-built portfolio."""
    after = _actual_values(session, broker)
    before = dict(after)
    done = session.scalars(select(TradeProposal).where(TradeProposal.created_at == proposal.created_at,
                                                       TradeProposal.status == ProposalStatus.EXECUTED))
    for sibling in done:
        before[sibling.ticker] = max(before.get(sibling.ticker, ZERO) - sibling.dollar_amount, ZERO)
    for other in committed_proposals(session):             # still-open legs of the same plan, counted as done
        if other.id != proposal.id and other.side == "buy" and other.created_at == proposal.created_at:
            after[other.ticker] = after.get(other.ticker, ZERO) + other.dollar_amount
            before[other.ticker] = before.get(other.ticker, ZERO) + other.dollar_amount
    dust = Decimal("0.05")                                  # market-value drift must not look like a real holding
    before = {t: v for t, v in before.items() if v >= dust}
    return after, before


def execute_proposal(session: Session, proposal: TradeProposal, broker: Broker, profile: policy.Profile,
                     now: datetime | None = None) -> TradeProposal:
    """Send an approved proposal to the broker and book the fill. Re-checks that the cash exists."""
    now = now or utcnow()
    try:
        if proposal.dollar_amount > available_balance(session):
            raise InsufficientFunds("pot no longer covers the order")
        fill = broker.buy_market(proposal.ticker, proposal.dollar_amount)
        consume_pot(session, proposal.dollar_amount)
        apply_fill(session, proposal.ticker, fill.quantity, fill.price, now)
        proposal.status, proposal.executed_at = ProposalStatus.EXECUTED, now
        after, before = _batch_values(session, broker, proposal)
        target = profile.targets[profile.core_ticker]
        log_event(session, "trade_executed",
                  explain.bought(proposal.ticker, proposal.dollar_amount, policy.core_weight(profile, before),
                                 policy.core_weight(profile, after), target, profile.band_low, profile.band_high),
                  {"proposal_id": proposal.id, "quantity": fill.quantity, "price": fill.price,
                   "order_id": fill.order_id}, now)
        snapshot_now(session, broker, now)                   # history point on every executed trade
    except Exception as exc:                                 # noqa: BLE001  never leave a proposal stuck
        logger.warning("execution of proposal %s failed: %s", proposal.id, exc)
        proposal.status, proposal.policy_reason = ProposalStatus.FAILED, "EXECUTION_FAILED"
        log_event(session, "trade_failed", explain.execution_failed(proposal.ticker, proposal.dollar_amount, str(exc)[:160]),
                  {"proposal_id": proposal.id}, now)
    session.flush()
    return proposal


def process_due(session: Session, broker: Broker, profile: policy.Profile, now: datetime | None = None) -> list[TradeProposal]:
    """Execute every APPROVED proposal whose undo window has passed."""
    now = now or utcnow()
    due = list(session.scalars(select(TradeProposal).where(
        TradeProposal.status == ProposalStatus.APPROVED, TradeProposal.undo_deadline <= now).order_by(TradeProposal.id)))
    for prop in due:
        execute_proposal(session, prop, broker, profile, now)
    return due


# ------------------------------------------------------------------ account value & history
def total_cashback(session: Session) -> Decimal:
    return sum((c.cashback_amount for c in session.scalars(select(CashbackCredit))), ZERO)


def withdrawn_total(session: Session) -> Decimal:
    """Money that left the account through payouts (failed payouts came back, so they don't count)."""
    return sum((p.amount for p in session.scalars(select(Payout).where(Payout.status != PayoutStatus.FAILED))), ZERO)


def net_contributions(session: Session) -> Decimal:
    return total_cashback(session) - withdrawn_total(session)


def holdings_values(session: Session, quotes: Mapping[str, Quote]) -> dict[str, Decimal]:
    """Actual market value per held ticker (cost basis if a quote is missing)."""
    return {p.ticker: p.quantity * (quotes[p.ticker].price if p.ticker in quotes else p.avg_cost)
            for p in get_positions(session)}


def record_snapshot(session: Session, quotes: Mapping[str, Quote], now: datetime | None = None,
                    min_interval_seconds: int = 10, force: bool = False) -> PortfolioSnapshot | None:
    """Append a point to the account-value history (throttled; nothing is recorded for an empty account)."""
    now = now or utcnow()
    positions_value = sum(holdings_values(session, quotes).values(), ZERO)
    cash = available_balance(session)
    last = session.scalar(select(PortfolioSnapshot).order_by(PortfolioSnapshot.id.desc()).limit(1))
    if last is None and positions_value == 0 and cash == 0:
        return None
    if last is not None and not force and (now - last.created_at).total_seconds() < min_interval_seconds:
        return None
    snap = PortfolioSnapshot(total_value=positions_value + cash, invested_cost=net_contributions(session),
                             cash=cash, created_at=now)
    session.add(snap)
    session.flush()
    return snap


def snapshot_now(session: Session, broker: Broker, now: datetime | None = None) -> PortfolioSnapshot | None:
    """Force a history point using fresh quotes (called after every executed trade, capture and payout)."""
    held = [p.ticker for p in get_positions(session)]
    return record_snapshot(session, prices.get_quotes(broker, held) if held else {}, now, force=True)


# ------------------------------------------------------------------ selling to withdraw
def apply_sell(session: Session, ticker: str, quantity: Decimal, proceeds: Decimal, now: datetime) -> None:
    pos = session.get(Position, ticker)
    if pos is None or quantity > pos.quantity + Decimal("0.000002"):
        raise InsufficientFunds(f"not enough {ticker} to sell")
    remaining = pos.quantity - quantity
    if remaining <= Decimal("0.000002"):
        session.delete(pos)
    else:
        pos.quantity, pos.updated_at = units(remaining), now
    credit_pot(session, proceeds, "sale", now)


def sell_for_withdrawal(session: Session, broker: Broker, amount: Decimal, now: datetime | None = None) -> list[TradeProposal]:
    """Raise cash for a USER-INITIATED withdrawal by selling investments pro rata.

    Only the withdrawal endpoint calls this: the user's request is the approval the policy engine
    requires for sells. Each sell is still checked (fresh quote) before it is sent.
    """
    now = now or utcnow()
    need = usd(amount) - uncommitted_pot(session)
    if need <= 0:
        return []
    positions = get_positions(session)
    quotes = prices.get_quotes(broker, [p.ticker for p in positions])
    missing = [p.ticker for p in positions if p.ticker not in quotes]
    if missing:
        raise InsufficientFunds(f"no price available for {', '.join(missing)}")
    values = {p.ticker: p.quantity * quotes[p.ticker].price for p in positions}
    total = sum(values.values(), ZERO)
    if total < BROKER_MIN_NOTIONAL or need > total + Decimal("0.01"):
        raise InsufficientFunds(f"only {explain.money(total)} is invested, so {explain.money(amount)} cannot be raised")
    need = min(max(need, BROKER_MIN_NOTIONAL), total)
    shares = {t: need * v / total for t, v in values.items()}
    biggest = max(values, key=lambda t: values[t])
    for t in list(shares):                                   # fold sub-$1 slices into the biggest holding
        if t != biggest and shares[t] < BROKER_MIN_NOTIONAL and shares[t] < values[t] * Decimal("0.98"):
            shares[biggest] = min(shares[biggest] + shares[t], values[biggest])
            shares[t] = ZERO
    snapshot = LedgerSnapshot(now=now, pot_available=uncommitted_pot(session), positions_value=dict(values), quotes=quotes)
    sold: list[TradeProposal] = []
    for pos in positions:
        share = shares.get(pos.ticker, ZERO)
        if share <= 0:
            continue
        verdict = policy.validate_withdrawal_sell(pos.ticker, usd(share), snapshot)
        if verdict.status.value == "REJECTED":
            raise SellRejected(explain.rejected(verdict))
        if share >= values[pos.ticker] * Decimal("0.98"):
            fill = broker.sell_market(pos.ticker, quantity=pos.quantity)       # sell the whole position, no dust
        else:
            fill = broker.sell_market(pos.ticker, dollar_amount=usd(share))
        apply_sell(session, pos.ticker, fill.quantity, fill.notional, now)
        row = TradeProposal(ticker=pos.ticker, dollar_amount=fill.notional, side="sell", status=ProposalStatus.EXECUTED,
                            policy_reason="WITHDRAWAL_SELL", llm_rationale="User-initiated withdrawal",
                            created_at=now, executed_at=now)
        session.add(row)
        session.flush()
        log_event(session, "trade_executed", explain.sold(pos.ticker, fill.notional),
                  {"proposal_id": row.id, "quantity": fill.quantity, "price": fill.price, "order_id": fill.order_id}, now)
        sold.append(row)
    return sold
