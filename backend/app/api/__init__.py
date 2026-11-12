"""API layer: shared dependencies (service container, DB session)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.agent.loop import LLMClient
from app.brokerage.base import Broker
from app.channel3 import Channel3Client
from app.config import Settings
from app.paypal.client import PayPalClient


@dataclass
class Services:
    settings: Settings
    session_factory: Callable[[], Session]
    broker: Broker
    llm: LLMClient | None
    paypal: PayPalClient
    channel3: Channel3Client | None = None


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_session(services: Services = Depends(get_services)) -> Iterator[Session]:
    session = services.session_factory()
    try:
        yield session
    finally:
        session.close()


_BROKER_LABELS = {"simulated": "Simulated market (demo prices)", "alpaca": "Alpaca paper trading"}


def broker_info(services: Services) -> tuple[str, str, bool]:
    """(kind, human label, is_simulated) for whichever broker is actually in use."""
    kind = getattr(services.broker, "kind", "custom")
    return kind, _BROKER_LABELS.get(kind, kind), kind == "simulated"
