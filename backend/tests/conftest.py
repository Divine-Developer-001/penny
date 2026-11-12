"""Shared fixtures: in-memory SQLite, simulated broker, fake PayPal and fake LLM. No network, ever."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import Services
from app.brokerage.sim_broker import SimBroker
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.main import create_app

NOW = datetime(2026, 11, 4, 12, 0, 0)


def make_settings(**overrides: Any) -> Settings:
    base = dict(broker_mode="simulated", llm_api_key="", undo_window_seconds=0, paypal_webhook_id="WEBHOOK_ID",
                paypal_env="sandbox", paypal_client_id="cid-real", paypal_client_secret="secret-real",
                paypal_payout_receiver_email="buyer@example.com",
                auto_agent_on_purchase=False)
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakePayPal:
    """Stands in for PayPalClient: records calls and returns canned sandbox-shaped responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.item_status = "PENDING"
        self.fail_create = False
        self.orders: dict[str, str] = {}              # order id -> amount value
        self.capture_status = "COMPLETED"
        self.already_captured = False

    def request(self, method: str, path: str, *, json_body: Any = None, content: str | None = None) -> dict[str, Any]:
        self.calls.append((method, path, json_body if json_body is not None else content))
        if method == "POST" and path == "/v1/payments/payouts":
            if self.fail_create:
                raise RuntimeError("PayPal is down")
            return {"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "PENDING"}}
        if method == "POST" and path == "/v2/checkout/orders":
            oid = f"ORDER{len(self.orders) + 1}"
            self.orders[oid] = json_body["purchase_units"][0]["amount"]["value"]
            return {"id": oid, "status": "CREATED"}
        if path.startswith("/v2/checkout/orders/"):
            oid = path.split("/")[4]
            body = {"id": oid, "status": self.capture_status, "purchase_units": [{"payments": {"captures": [
                {"id": f"CAP-{oid}", "status": self.capture_status, "amount": {"currency_code": "USD", "value": self.orders[oid]}}]}}]}
            if method == "POST" and path.endswith("/capture") and self.already_captured:
                from app.paypal.client import PayPalError
                raise PayPalError("POST capture -> HTTP 422: ORDER_ALREADY_CAPTURED")
            return body
        if method == "GET" and path == "/v1/payments/payouts/BATCH1":
            return {"items": [{"payout_item_id": "ITEM1"}]}
        if method == "GET" and path == "/v1/payments/payouts-item/ITEM1":
            return {"payout_item_id": "ITEM1", "transaction_status": self.item_status}
        return {}


class FakeChannel3:
    """Stands in for Channel3Client. `rows` are returned as live offers; `error` simulates an outage."""

    def __init__(self, rows=None, error=None, configured=True) -> None:
        self.rows, self.error, self.configured, self.queries = rows or [], error, configured, []

    def top_offers(self, query, limit=3):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.rows[:limit]


class FakeLLM:
    """Scripted chat-completions: each call pops the next assistant message."""

    def __init__(self, script: list[dict[str, Any] | Exception]) -> None:
        self.script, self.seen = list(script), []

    def chat(self, messages, tools):
        self.seen.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> dict[str, Any]:
    import json
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def assistant(*calls: dict[str, Any], content: str | None = None) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def fake_paypal() -> FakePayPal:
    return FakePayPal()


@pytest.fixture
def services(settings, session_factory, fake_paypal) -> Services:
    return Services(settings=settings, session_factory=session_factory, broker=SimBroker(), llm=None, paypal=fake_paypal,
                    channel3=FakeChannel3(configured=False))


@pytest.fixture
def client(services) -> TestClient:
    return TestClient(create_app(services, enable_scheduler=False, frontend_dist=None))
