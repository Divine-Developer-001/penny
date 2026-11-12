"""Pricing layer: latest quotes via the broker; a failing quote is omitted, never invented."""
from __future__ import annotations

import logging
from typing import Iterable

from app.brokerage.base import Broker
from app.schemas import Quote

logger = logging.getLogger(__name__)


def get_quotes(broker: Broker, tickers: Iterable[str]) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for ticker in sorted({t.upper() for t in tickers}):
        try:
            out[ticker] = broker.quote(ticker)
        except Exception as exc:                             # noqa: BLE001
            logger.warning("no quote for %s: %s", ticker, exc)
    return out
