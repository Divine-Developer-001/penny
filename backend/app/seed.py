"""Demo seed layer: about 30 days of consistent, clearly-labelled DEMO history (idempotent).

Creates 40 cashback credits ($0.30-$2.00), replays the real threshold/rebalancing rules to produce
5-10 executed buys, writes realistic one-line explanations, and hourly portfolio snapshots, so the
dashboard looks alive. Prices are a seeded random walk that ends exactly at the broker's current
quote, so the history joins up with live prices. Nothing here is a real transaction.
Run manually:  python -m backend.app.seed   (from the repo root)
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cashback, catalog, explain, ledger, policy, prices, threshold
from app.brokerage.base import Broker
from app.brokerage.sim_broker import BASE_PRICES
from app.config import Settings
from app.models import (CashbackCredit, LedgerEvent, Payout, PendingCash, PortfolioSnapshot, Position, ProposalStatus,
                        TradeProposal)
from app.schemas import LedgerSnapshot, Rules, units, usd, utcnow

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
HOURS = 30 * 24
N_CREDITS = 40


def has_seed_data(session: Session) -> bool:
    if ledger.get_setting(session, "SEED_APPLIED") == "true":
        return True
    return any(session.scalar(select(m).limit(1)) is not None
               for m in (CashbackCredit, Position, PendingCash, LedgerEvent, Payout))


def price_paths(tickers: list[str], current: dict[str, Decimal], seed: int) -> dict[str, list[Decimal]]:
    """Hourly price paths (HOURS+1 points) ending exactly at `current`: a seeded random walk with a shared market factor."""
    common = random.Random(f"{seed}:market")
    market = [common.gauss(0.00003, 0.0022) for _ in range(HOURS)]
    out: dict[str, list[Decimal]] = {}
    for t in tickers:
        own = random.Random(f"{seed}:{t}")
        cum = [0.0]
        for m in market:
            cum.append(cum[-1] + 0.7 * m + 0.7 * own.gauss(0, 0.0018))
        last = cum[-1]
        out[t] = [units(current[t] * Decimal(str(math.exp(c - last)))) for c in cum]
    return out


def seed_demo(session: Session, broker: Broker, settings: Settings, now: datetime | None = None, seed: int = 7) -> bool:
    """Write the demo history. Returns False (and writes nothing) if the database already has data."""
    if has_seed_data(session):
        return False
    now = now or utcnow()
    start = now - timedelta(hours=HOURS)
    rules, profile = Rules.from_settings(settings), ledger.get_profile(session, settings)
    tickers = list(profile.targets)
    quotes = prices.get_quotes(broker, tickers)
    current = {t: (quotes[t].price if t in quotes else BASE_PRICES.get(t, Decimal("100"))) for t in tickers}
    paths = price_paths(tickers, current, seed)

    def px(ticker: str, when: datetime) -> Decimal:
        return paths[ticker][max(0, min(HOURS, int((when - start).total_seconds() // 3600)))]

    rng = random.Random(seed)
    times = sorted(start + timedelta(seconds=rng.uniform(3600, (HOURS - 6) * 3600)) for _ in range(N_CREDITS))
    min_pot = threshold.min_invest_amount(rules)
    target = profile.targets[profile.core_ticker]
    ledger.log_event(session, "seed", "Demo data loaded: about 30 days of simulated history (not real transactions).",
                     {"seeded": True}, start)

    moves: list[tuple[datetime, Decimal, dict[str, Decimal], Decimal]] = []     # (when, cash, qty deltas, contribution)
    qty = {t: ZERO for t in tickers}
    pot, pot_since = ZERO, None
    for i, when in enumerate(times):
        wanted = Decimal(rng.randint(30, 200)) / 100
        credit = cashback.apply(session, settings, f"seed-{i}", usd(wanted / settings.cashback_rate), now=when)
        if credit is None or credit.cashback_amount <= 0:
            continue
        pot += credit.cashback_amount
        pot_since = pot_since or when
        moves.append((when, credit.cashback_amount, {}, credit.cashback_amount))

        if pot >= min_pot:
            values = {t: qty[t] * px(t, when) for t in tickers}
            orders = threshold.plan_rebalance(LedgerSnapshot(now=when, pot_available=pot, positions_value=values), profile, pot)
            before, fills = dict(values), []
            for j, order in enumerate(orders):
                at = when + timedelta(minutes=2 + j)
                price = px(order.ticker, at)
                q = units(order.dollar_amount / price)
                ledger.consume_pot(session, order.dollar_amount)
                ledger.apply_fill(session, order.ticker, q, price, at)
                qty[order.ticker] += q
                session.add(TradeProposal(ticker=order.ticker, dollar_amount=order.dollar_amount, side="buy",
                                          status=ProposalStatus.EXECUTED, policy_reason="OK",
                                          llm_rationale="Seeded demo order", created_at=at, executed_at=at))
                moves.append((at, -order.dollar_amount, {order.ticker: q}, ZERO))
                fills.append((at, order, price))
            after = {t: qty[t] * px(t, when + timedelta(minutes=5)) for t in tickers}
            for at, order, price in fills:
                ledger.log_event(session, "trade_executed",
                                 explain.bought(order.ticker, order.dollar_amount, policy.core_weight(profile, before),
                                                policy.core_weight(profile, after), target, profile.band_low, profile.band_high),
                                 {"seeded": True, "price": price}, at)
            pot, pot_since = ZERO, None
        elif i % 5 == 0:
            age = Decimal(str((when - pot_since).total_seconds() / 86400))
            ledger.log_event(session, "held", explain.held(pot, age, rules.fee_assumed, min_pot), {"seeded": True}, when)

    moves.sort(key=lambda m: m[0])
    cash = contrib = ZERO
    run_qty, k = {t: ZERO for t in tickers}, 0
    for h in range(1, HOURS):                                              # hourly snapshots; the last point is "now"
        t_h = start + timedelta(hours=h)
        while k < len(moves) and moves[k][0] <= t_h:
            _, d_cash, d_qty, d_contrib = moves[k]
            cash, contrib = cash + d_cash, contrib + d_contrib
            for t, dq in d_qty.items():
                run_qty[t] += dq
            k += 1
        if contrib > 0:
            value = cash + sum(run_qty[t] * paths[t][h] for t in tickers)
            session.add(PortfolioSnapshot(total_value=usd(value), invested_cost=usd(contrib), cash=usd(cash), created_at=t_h))
    session.flush()
    ledger.snapshot_now(session, broker, now)
    ledger.set_setting(session, "SEED_APPLIED", "true")
    logger.info("demo seed written: %d credits, %d positions", N_CREDITS, len(ledger.get_positions(session)))
    return True


def main() -> int:
    """CLI: create tables, ensure products, apply the demo seed once."""
    from app.config import get_settings
    from app.main import build_services

    settings = get_settings()
    services = build_services(settings)
    with services.session_factory() as session:
        catalog.ensure_products(session)
        did = seed_demo(session, services.broker, settings)
        session.commit()
    print("Demo seed applied." if did else "Database already has data; nothing seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
