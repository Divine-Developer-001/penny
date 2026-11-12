"""Brokerage layer: the Broker protocol every execution backend implements."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.schemas import Quote


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fill:
    ticker: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    order_id: str


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    quantity: Decimal


@runtime_checkable
class Broker(Protocol):
    def quote(self, ticker: str) -> Quote: ...
    def buy_market(self, ticker: str, dollar_amount: Decimal) -> Fill: ...
    def sell_market(self, ticker: str, *, dollar_amount: Decimal | None = None,
                    quantity: Decimal | None = None) -> Fill: ...
    def positions(self) -> dict[str, BrokerPosition]: ...
