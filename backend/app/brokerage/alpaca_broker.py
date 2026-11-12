"""Brokerage layer: Alpaca PAPER trading via alpaca-py. Paper only: no real money, no real securities."""
from __future__ import annotations

import logging
import time
from datetime import timezone
from decimal import Decimal

from app.brokerage.base import BrokerError, BrokerPosition, Fill
from app.schemas import Quote

logger = logging.getLogger(__name__)
_TERMINAL_BAD = {"canceled", "expired", "rejected", "suspended"}


class AlpacaBroker:
    kind = "alpaca"

    def __init__(self, api_key: str, secret_key: str, fill_timeout_seconds: int = 20) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading = TradingClient(api_key, secret_key, paper=True)      # paper=True is hard-wired
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._timeout = fill_timeout_seconds

    def quote(self, ticker: str) -> Quote:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        ticker = ticker.upper()
        trade = self._data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX))[ticker]
        ts = trade.timestamp
        as_of = ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts
        return Quote(ticker, Decimal(str(trade.price)), as_of)

    def _submit_and_wait(self, request, ticker: str, notional: Decimal | None) -> Fill:
        order = self._trading.submit_order(request)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            o = self._trading.get_order_by_id(order.id)
            status = str(getattr(o.status, "value", o.status)).lower()
            if status == "filled":
                qty, price = Decimal(str(o.filled_qty)), Decimal(str(o.filled_avg_price))
                return Fill(ticker.upper(), qty, price, notional or (qty * price).quantize(Decimal("0.01")), str(o.id))
            if status in _TERMINAL_BAD:
                raise BrokerError(f"order {status}")
            time.sleep(1.0)
        try:
            self._trading.cancel_order_by_id(order.id)
        except Exception:                                    # noqa: BLE001
            logger.warning("could not cancel unfilled order %s", order.id)
        raise BrokerError(f"order not filled within {self._timeout}s (is the market open?)")

    def buy_market(self, ticker: str, dollar_amount: Decimal) -> Fill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=ticker.upper(), notional=float(dollar_amount), side=OrderSide.BUY,
                                 time_in_force=TimeInForce.DAY)
        return self._submit_and_wait(req, ticker, dollar_amount)

    def sell_market(self, ticker: str, *, dollar_amount: Decimal | None = None,
                    quantity: Decimal | None = None) -> Fill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if quantity is not None:
            req = MarketOrderRequest(symbol=ticker.upper(), qty=float(quantity), side=OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY)
            return self._submit_and_wait(req, ticker, None)
        if dollar_amount is None or dollar_amount <= 0:
            raise BrokerError("a positive dollar_amount or quantity is required")
        req = MarketOrderRequest(symbol=ticker.upper(), notional=float(dollar_amount), side=OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY)
        return self._submit_and_wait(req, ticker, dollar_amount)

    def positions(self) -> dict[str, BrokerPosition]:
        return {p.symbol: BrokerPosition(p.symbol, Decimal(str(p.qty))) for p in self._trading.get_all_positions()}
