"""Brokerage layer: simulated broker. Same interface as Alpaca; fills at the simulated quote.

Prices are a deterministic function of (ticker, time) around a base level, so demos and tests are
reproducible. The tickers are broad-index ETF *proxies*; the numbers are not real market data.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Callable

from app.brokerage.base import BrokerError, BrokerPosition, Fill
from app.schemas import Quote, units, utcnow

BASE_PRICES = {"VTI": Decimal("290.00"), "VXUS": Decimal("65.00"), "BND": Decimal("72.00")}


def default_price_fn(ticker: str, now: datetime) -> Decimal:
    """Smooth, market-looking movement: three sine waves plus 5-second noise, all deterministic."""
    base = BASE_PRICES.get(ticker)
    if base is None:
        raise BrokerError(f"no simulated price for {ticker}")
    t = now.timestamp()
    phase = sum(ord(c) for c in ticker)
    wave = (0.020 * math.sin(t / 600 + phase) + 0.008 * math.sin(t / 97 + phase * 2)
            + 0.003 * math.sin(t / 19 + phase * 3))
    noise = random.Random(f"{ticker}:{int(t // 5)}").uniform(-0.0012, 0.0012)
    return units(base * Decimal(str(1 + wave + noise)))


class SimBroker:
    kind = "simulated"

    def __init__(self, price_fn: Callable[[str, datetime], Decimal] = default_price_fn,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._price_fn = price_fn
        self._clock = clock
        self._positions: dict[str, Decimal] = {}

    def quote(self, ticker: str) -> Quote:
        now = self._clock()
        return Quote(ticker.upper(), self._price_fn(ticker.upper(), now), now)

    def buy_market(self, ticker: str, dollar_amount: Decimal) -> Fill:
        q = self.quote(ticker)
        if dollar_amount <= 0:
            raise BrokerError("notional must be positive")
        qty = units(dollar_amount / q.price)
        self._positions[q.ticker] = self._positions.get(q.ticker, Decimal(0)) + qty
        return Fill(q.ticker, qty, q.price, dollar_amount, f"sim-{uuid.uuid4().hex[:12]}")

    def sell_market(self, ticker: str, *, dollar_amount: Decimal | None = None,
                    quantity: Decimal | None = None) -> Fill:
        q = self.quote(ticker)
        if quantity is None:
            if dollar_amount is None or dollar_amount <= 0:
                raise BrokerError("a positive dollar_amount or quantity is required")
            quantity = units(dollar_amount / q.price)
            notional = dollar_amount
        else:
            notional = (quantity * q.price).quantize(Decimal("0.01"))
        self._positions[q.ticker] = self._positions.get(q.ticker, Decimal(0)) - quantity
        return Fill(q.ticker, quantity, q.price, notional, f"sim-{uuid.uuid4().hex[:12]}")

    def positions(self) -> dict[str, BrokerPosition]:
        return {t: BrokerPosition(t, q) for t, q in self._positions.items()}
