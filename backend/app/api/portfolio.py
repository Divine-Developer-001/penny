"""API layer: the widget's data. GET /api/state (everything in one poll), /events, /explain, /history."""
from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import explain, ledger, policy, prices, threshold
from app.api import Services, broker_info, get_services, get_session
from app.models import LedgerEvent, Payout, PortfolioSnapshot, ProposalStatus, Purchase, TradeProposal
from app.schemas import (ActivityOut, BrokerOut, EventOut, HistoryPointOut, HoldingOut, PayoutOut, PotOut, ProposalOut,
                         PurchaseOut, Rules, StateOut, TotalsOut, usd, utcnow)

router = APIRouter()
ZERO = Decimal("0")


def _status_message(snapshot, decision, queued: int, pending: int, fee: Decimal) -> str:
    if queued:
        return f"{queued} order{'s' if queued != 1 else ''} waiting to execute. You can undo {'them' if queued != 1 else 'it'} first."
    if pending:
        return f"{pending} order{'s' if pending != 1 else ''} above your auto-approve cap {'are' if pending != 1 else 'is'} waiting for you."
    pot = snapshot.pot_available
    if pot <= 0:
        return "Nothing waiting. Buy something and Penny collects the cashback for you."
    if decision.invest:
        return f"{explain.money(pot)} is ready to invest; Penny will place the orders on its next run."
    if decision.code in ("BELOW_THRESHOLD", "POT_BELOW_BROKER_MINIMUM"):
        return explain.held(pot, snapshot.oldest_pending_age_days, fee, decision.threshold)
    return decision.detail


@router.get("/state", response_model=StateOut)
def state(services: Services = Depends(get_services), session: Session = Depends(get_session)) -> StateOut:
    now = utcnow()
    profile = ledger.get_profile(session, services.settings)
    rules = Rules.from_settings(services.settings)
    positions = ledger.get_positions(session)
    quotes = prices.get_quotes(services.broker, list(profile.targets) + [p.ticker for p in positions])

    values = ledger.holdings_values(session, quotes)
    positions_value = sum(values.values(), ZERO)
    cash, in_transit = ledger.available_balance(session), ledger.in_transit_balance(session)
    cashback_total, withdrawn = ledger.total_cashback(session), ledger.withdrawn_total(session)
    total_value = positions_value + cash
    pl = total_value - (cashback_total - withdrawn)
    net = cashback_total - withdrawn
    snapshot = ledger.build_snapshot(session, quotes, now)

    holdings = []
    for p in positions:
        value, cost = values[p.ticker], p.quantity * p.avg_cost
        holdings.append(HoldingOut(
            ticker=p.ticker, name=policy.TICKER_NAMES.get(p.ticker, p.ticker), quantity=p.quantity, avg_cost=p.avg_cost,
            price=quotes[p.ticker].price if p.ticker in quotes else None, value=usd(value),
            weight=(value / positions_value) if positions_value > 0 else ZERO, target_weight=profile.targets.get(p.ticker),
            profit_loss=usd(value - cost), profit_loss_pct=usd((value - cost) / cost * 100) if cost > 0 else ZERO))

    def proposals(status: str):
        return list(session.scalars(select(TradeProposal).where(TradeProposal.status == status).order_by(TradeProposal.id)))

    queued, pending = proposals(ProposalStatus.APPROVED), proposals(ProposalStatus.PENDING_APPROVAL)
    decision = threshold.decide(snapshot, profile, rules)
    kind, label, simulated = broker_info(services)
    events = session.scalars(select(LedgerEvent).where(LedgerEvent.kind != "webhook_ignored").order_by(LedgerEvent.id.desc()).limit(40))
    purchases = session.scalars(select(Purchase).where(Purchase.status == "CAPTURED").order_by(Purchase.id.desc()).limit(5))
    recent = session.scalars(select(TradeProposal).order_by(TradeProposal.id.desc()).limit(10))
    payout = session.scalar(select(Payout).order_by(Payout.id.desc()).limit(1))

    return StateOut(
        broker=BrokerOut(mode=kind, label=label, simulated=simulated), profile=profile.name,
        profile_targets=profile.targets, core_ticker=profile.core_ticker, core_band=[profile.band_low, profile.band_high],
        pot=PotOut(available=cash, in_transit=in_transit, committed=ledger.committed_amount(session),
                   uncommitted=ledger.uncommitted_pot(session)),
        totals=TotalsOut(cashback_total=usd(cashback_total), withdrawn=usd(withdrawn), cash=usd(cash), in_transit=usd(in_transit),
                         positions_value=usd(positions_value), total_value=usd(total_value), profit_loss=usd(pl),
                         profit_loss_pct=usd(pl / net * 100) if net > 0 else ZERO,
                         max_withdrawable=usd(ledger.uncommitted_pot(session) + positions_value)),
        holdings=holdings, purchases=[PurchaseOut.model_validate(p) for p in purchases],
        activity=[ActivityOut(kind=e.kind, description=e.description, created_at=e.created_at) for e in events],
        queued_orders=[ProposalOut.model_validate(p) for p in queued], pending_approvals=[ProposalOut.model_validate(p) for p in pending],
        recent_proposals=[ProposalOut.model_validate(p) for p in recent],
        latest_payout=PayoutOut.model_validate(payout) if payout else None,
        status_message=_status_message(snapshot, decision, len(queued), len(pending), rules.fee_assumed),
        invest_threshold=threshold.min_invest_amount(rules))


@router.get("/events", response_model=list[EventOut])
def events(limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)) -> list[LedgerEvent]:
    return list(session.scalars(select(LedgerEvent).order_by(LedgerEvent.id.desc()).limit(limit)))


@router.get("/explain", response_model=list[EventOut])
def explain_log(session: Session = Depends(get_session)) -> list[LedgerEvent]:
    """The last 50 one-line explanations, newest first."""
    return list(session.scalars(select(LedgerEvent).order_by(LedgerEvent.id.desc()).limit(50)))


@router.get("/history", response_model=list[HistoryPointOut])
def history(days: int | None = Query(None, ge=1, le=365), services: Services = Depends(get_services),
            session: Session = Depends(get_session)) -> list[HistoryPointOut]:
    """[{t, value, invested_cost}] for the chart: stored snapshots plus one live point computed right now."""
    query = select(PortfolioSnapshot).order_by(PortfolioSnapshot.created_at, PortfolioSnapshot.id)
    if days:
        query = query.where(PortfolioSnapshot.created_at >= utcnow() - timedelta(days=days))
    snaps = list(session.scalars(query))
    if len(snaps) > 800:
        step = math.ceil(len(snaps) / 800)
        snaps = snaps[::step] + ([snaps[-1]] if (len(snaps) - 1) % step else [])
    points = [HistoryPointOut(t=s.created_at, value=usd(s.total_value), invested_cost=usd(s.invested_cost)) for s in snaps]
    held = [p.ticker for p in ledger.get_positions(session)]
    quotes = prices.get_quotes(services.broker, held) if held else {}
    live_value = sum(ledger.holdings_values(session, quotes).values(), ZERO) + ledger.available_balance(session)
    if live_value > 0 or points:                                  # the live point keeps the line moving between snapshots
        points.append(HistoryPointOut(t=utcnow(), value=usd(live_value), invested_cost=usd(ledger.net_contributions(session))))
    return points
