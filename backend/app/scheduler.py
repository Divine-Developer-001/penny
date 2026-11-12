"""Scheduler layer: periodic agent run, due-proposal execution and payout polling (APScheduler)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app import ledger, prices
from app.agent.loop import run_agent
from app.api import Services
from app.paypal import payouts

logger = logging.getLogger(__name__)


def run_agent_job(services: Services) -> None:
    with services.session_factory() as session:
        try:
            result = run_agent(session, services.settings, services.broker, services.llm)
            logger.info("agent run: %s", {k: result[k] for k in ("mode", "iterations", "skipped")})
        except Exception:                                     # noqa: BLE001
            session.rollback()
            logger.exception("agent run failed")


def snapshot_job(services: Services) -> None:
    """Record account value for the dashboard chart."""
    with services.session_factory() as session:
        try:
            held = [p.ticker for p in ledger.get_positions(session)]
            quotes = prices.get_quotes(services.broker, held) if held else {}
            if ledger.record_snapshot(session, quotes, min_interval_seconds=600):
                session.commit()
        except Exception:                                     # noqa: BLE001
            session.rollback()
            logger.exception("snapshot failed")


def process_due_job(services: Services) -> None:
    with services.session_factory() as session:
        try:
            profile = ledger.get_profile(session, services.settings)
            if ledger.process_due(session, services.broker, profile):
                held = [p.ticker for p in ledger.get_positions(session)]
                ledger.record_snapshot(session, prices.get_quotes(services.broker, held), force=True)
                session.commit()
        except Exception:                                     # noqa: BLE001
            session.rollback()
            logger.exception("processing due proposals failed")


def poll_payouts_job(services: Services) -> None:
    with services.session_factory() as session:
        try:
            if payouts.poll_payouts(session, services.paypal):
                session.commit()
        except Exception:                                     # noqa: BLE001
            session.rollback()
            logger.exception("payout polling failed")


def start_scheduler(services: Services) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    common = {"max_instances": 1, "coalesce": True}
    sched.add_job(run_agent_job, "interval", minutes=max(services.settings.agent_interval_minutes, 1),
                  args=[services], id="agent", **common)
    sched.add_job(snapshot_job, "interval", hours=1, args=[services], id="snapshots",
                  next_run_time=datetime.now(timezone.utc), **common)
    sched.add_job(process_due_job, "interval", seconds=5, args=[services], id="due", **common)
    sched.add_job(poll_payouts_job, "interval", seconds=60, args=[services], id="payouts", **common)
    sched.start()
    return sched
