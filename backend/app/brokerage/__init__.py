"""Brokerage layer: factory choosing Alpaca paper trading or the simulated broker."""
from __future__ import annotations

import logging

from app.brokerage.base import Broker
from app.config import Settings

logger = logging.getLogger(__name__)


def make_broker(settings: Settings) -> Broker:
    """Alpaca paper when configured and requested; otherwise the simulated fallback."""
    from app.brokerage.sim_broker import SimBroker

    if settings.broker_mode.lower() == "alpaca":
        if settings.alpaca_configured:
            from app.brokerage.alpaca_broker import AlpacaBroker
            return AlpacaBroker(settings.alpaca_api_key_id, settings.alpaca_secret_key)
        logger.warning("BROKER_MODE=alpaca but no real Alpaca keys found; using the simulated broker")
    return SimBroker()
