"""Web app backend: store + deals, Channel3, PayPal sandbox checkout, state/history, demo seed, withdrawing investments."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import catalog, ledger, seed
from app.agent.loop import run_agent
from app.channel3 import Channel3Client, Channel3Error, _price
from app.db import normalize_url
from app.models import CashbackCredit, LedgerEvent, Payout, PortfolioSnapshot, Position, Product, Purchase, TradeProposal
from app.paypal.webhooks import VerifyResult
from app.schemas import LedgerSnapshot, Quote, utcnow
from tests.conftest import FakeChannel3, make_settings


def buy(client, product="sw-pulse", mode="simulated"):
    order = client.post("/api/orders", json={"product_id": product, "mode": mode})
    assert order.status_code == 200, order.text
    return order.json(), client.post(f"/api/orders/{order.json()['order_id']}/capture")


def state(client):
    return client.get("/api/state").json()


# ------------------------------------------------------------------ store & deals
def test_products_are_stored_in_the_database_and_listed(client, services):
    items = client.get("/api/products").json()
    assert len(items) == 10 and {"id", "name", "description", "image_url", "price", "category"} <= set(items[0])
    with services.session_factory() as s:
        assert len(s.scalars(select(Product)).all()) == 10
        assert catalog.ensure_products(s) == 0                                    # idempotent
    assert [p["id"] for p in client.get("/api/products", params={"q": "coffee"}).json()] == ["cm-brew"]
    assert client.get("/api/products", params={"q": "zzzz"}).json() == []


def test_product_images_are_served_without_external_assets(client):
    r = client.get("/api/products/hp-aurora/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml" and "<svg" in r.text
    assert client.get("/api/products/nope/image").status_code == 404
    assert client.get("/api/products").json()[0]["image_url"].startswith("/api/products/")


def test_sample_offers_are_deterministic_and_sorted():
    a = catalog.sample_offers("hp-aurora", "Aurora", D("89.99"))
    assert a == catalog.sample_offers("hp-aurora", "Aurora", D("89.99")) and len(a) == 3
    assert [o["price"] for o in a] == sorted(o["price"] for o in a) and all(o["link"] is None for o in a)


def test_deals_fall_back_to_samples_without_a_channel3_key(client):
    d = client.get("/api/deals", params={"product_id": "hp-aurora"}).json()
    assert d["sample"] is True and len(d["offers"]) == 3 and D(d["store_price"]) == D("89.99")
    assert all(D(o["delta"]) == D(o["price"]) - D("89.99") for o in d["offers"])
    assert client.get("/api/deals", params={"product_id": "nope"}).status_code == 404
    assert client.get("/api/deals").status_code == 422


def test_deals_use_live_channel3_results_when_available(client, services):
    live = [{"title": "Aurora Headphones", "merchant": "shop.example", "price": D("79.00"), "link": "https://shop.example/a"},
            {"title": "Aurora Headphones", "merchant": "other.example", "price": D("95.50"), "link": "https://other.example/a"}]
    services.channel3 = FakeChannel3(rows=live)
    d = client.get("/api/deals", params={"product_id": "hp-aurora"}).json()
    assert d["sample"] is False and d["note"] is None and services.channel3.queries == ["Aurora Wireless Headphones"]
    assert [o["merchant"] for o in d["offers"]] == ["shop.example", "other.example"]
    assert D(d["offers"][0]["delta"]) == D("-10.99") and d["offers"][0]["link"] == "https://shop.example/a"


def test_deals_never_crash_when_channel3_fails_or_finds_nothing(client, services):
    services.channel3 = FakeChannel3(error=Channel3Error("Channel3 HTTP 401"))
    d = client.get("/api/deals", params={"product_id": "hp-aurora"}).json()
    assert d["sample"] is True and "unavailable" in d["note"]
    services.channel3 = FakeChannel3(rows=[])
    d = client.get("/api/deals", params={"product_id": "hp-aurora"}).json()
    assert d["sample"] is True and "no priced offers" in d["note"]


# ------------------------------------------------------------------ Channel3 client (verified request shape)
class _H:
    def __init__(self, status, body):
        self.status_code, self._b, self.text, self.sent = status, body, json.dumps(body), None

    def json(self):
        return self._b


class _HttpC:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        return self.resp


def test_channel3_request_uses_x_api_key_and_parses_offers():
    body = {"products": [
        {"title": "Nike Air Force 1", "offers": [
            {"domain": "nike.com", "url": "https://nike.com/af1", "price": {"price": 110.0, "currency": "USD"}},
            {"domain": "shop.test", "url": "https://shop.test/af1", "price": {"price": 95.5, "currency": "USD"}},
            {"domain": "euro.test", "url": "https://euro.test", "price": {"price": 90, "currency": "EUR"}}]},
        {"title": "Other", "offers": [{"domain": "x.test", "url": "https://x.test", "price": 70}]}]}
    http = _HttpC(_H(200, body))
    rows = Channel3Client(make_settings(channel3_api_key=" key123 "), http).top_offers("nike air force 1", limit=3)
    url, headers, sent = http.calls[0]
    assert url == "https://api.trychannel3.com/v1/search" and headers["x-api-key"] == "key123" and "Authorization" not in headers
    assert sent == {"query": "nike air force 1"}
    assert [(r["merchant"], r["price"]) for r in rows] == [("shop.test", D("95.50")), ("nike.com", D("110.00")), ("x.test", D("70.00"))]
    assert rows[0]["link"] == "https://shop.test/af1"                         # EUR offer skipped, cheapest first within a product


def test_channel3_errors_are_typed():
    with pytest.raises(Channel3Error, match="not set"):
        Channel3Client(make_settings(channel3_api_key=""), _HttpC(_H(200, {}))).top_offers("x")
    with pytest.raises(Channel3Error, match="HTTP 401"):
        Channel3Client(make_settings(channel3_api_key="k"), _HttpC(_H(401, {"detail": "invalid key"}))).top_offers("x")


def test_channel3_price_parsing():
    assert _price(49) == D("49.00") and _price("12.5") == D("12.50") and _price({"price": 3, "currency": "usd"}) == D("3.00")
    assert _price({"price": 3, "currency": "EUR"}) is None and _price(None) is None and _price("abc") is None and _price(0) is None


# ------------------------------------------------------------------ checkout
def test_simulated_checkout_credits_cashback_at_the_configured_rate(client, services):
    order, cap = buy(client, "hp-aurora")
    assert D(order["amount"]) == D("89.99") and order["order_id"].startswith("SIM-")
    body = cap.json()
    assert body["status"] == "COMPLETED" and D(body["cashback"]) == D("1.80") and body["message"] == "Cashback credited: $1.80 (simulated)"
    with services.session_factory() as s:
        assert ledger.available_balance(s) == D("1.80") and s.scalars(select(Purchase)).one().status == "CAPTURED"


def test_capture_is_idempotent(client, services):
    order, _ = buy(client)
    assert client.post(f"/api/orders/{order['order_id']}/capture").json()["duplicate"] is True
    with services.session_factory() as s:
        assert len(s.scalars(select(CashbackCredit)).all()) == 1


def test_the_price_always_comes_from_the_server(client):
    order = client.post("/api/orders", json={"product_id": "hp-aurora", "mode": "simulated", "amount": "0.01"}).json()
    assert D(order["amount"]) == D("89.99")
    assert client.post("/api/orders", json={"product_id": "nope", "mode": "simulated"}).status_code == 404
    assert client.post("/api/orders/NOPE/capture").status_code == 404


def test_paypal_checkout_creates_and_captures_a_sandbox_order(client, services, fake_paypal):
    order, cap = buy(client, "hp-aurora", mode="paypal")
    assert order["order_id"] == "ORDER1" and fake_paypal.orders["ORDER1"] == "89.99" and cap.status_code == 200
    with services.session_factory() as s:
        assert s.scalars(select(CashbackCredit)).one().paypal_event_id == "capture:CAP-ORDER1"


def test_paypal_mode_needs_credentials(client, services):
    services.settings.paypal_client_id = ""
    r = client.post("/api/orders", json={"product_id": "hp-aurora", "mode": "paypal"})
    assert r.status_code == 503 and "simulated" in r.json()["detail"]


def test_pending_paypal_captures_credit_nothing(client, services, fake_paypal):
    fake_paypal.capture_status = "PENDING"
    assert buy(client, mode="paypal")[1].status_code == 409
    with services.session_factory() as s:
        assert ledger.available_balance(s) == D("0")


def test_already_captured_orders_are_recovered_not_double_credited(client, services, fake_paypal):
    order = client.post("/api/orders", json={"product_id": "hp-aurora", "mode": "paypal"}).json()
    fake_paypal.already_captured = True
    assert client.post(f"/api/orders/{order['order_id']}/capture").status_code == 200
    with services.session_factory() as s:
        assert len(s.scalars(select(CashbackCredit)).all()) == 1


def test_a_webhook_for_the_same_capture_does_not_double_credit(client, services, monkeypatch):
    order, _ = buy(client, "hp-aurora", mode="paypal")
    monkeypatch.setattr("app.api.webhooks.verify_webhook", lambda *a, **k: VerifyResult(True, "self", ""))
    event = {"id": "WH-LATER", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAP-ORDER1", "amount": {"currency_code": "USD", "value": order["amount"]}}}
    assert client.post("/webhooks/paypal", content=json.dumps(event)).json()["detail"] == "duplicate"
    with services.session_factory() as s:
        assert len(s.scalars(select(CashbackCredit)).all()) == 1


def test_paypal_outage_when_creating_an_order_is_a_502(client, fake_paypal):
    from app.paypal.client import PayPalError

    def boom(*a, **k):
        raise PayPalError("down")
    fake_paypal.request = boom
    assert client.post("/api/orders", json={"product_id": "hp-aurora", "mode": "paypal"}).status_code == 502


def test_a_purchase_triggers_the_agent_only_when_the_pot_is_ready(session_factory, fake_paypal):
    from app.api import Services
    from app.brokerage.sim_broker import SimBroker
    from app.main import create_app
    svc = Services(make_settings(auto_agent_on_purchase=True, cashback_rate=D("0.10")), session_factory, SimBroker(), None, fake_paypal)
    c = TestClient(create_app(svc, enable_scheduler=False, frontend_dist=None))
    buy(c, "ym-flow")                                                          # $3.20 cashback: below the $10 threshold
    with svc.session_factory() as s:
        assert ledger.get_positions(s) == []
    for _ in range(3):
        buy(c, "sw-pulse")                                                     # pushes the pot over the threshold
    with svc.session_factory() as s:
        assert {p.ticker for p in ledger.get_positions(s)} == {"VTI", "VXUS"}


# ------------------------------------------------------------------ health, state, history
def test_health_reports_what_is_configured(client):
    h = client.get("/api/health").json()
    assert h == {"status": "ok", "broker": "simulated", "broker_label": "Simulated market (demo prices)", "paypal_configured": True,
                 "llm_configured": False, "channel3_configured": False, "seeded": False}


def invest(client, services, n=4):
    for _ in range(n):
        buy(client, "sw-pulse")
    with services.session_factory() as s:
        run_agent(s, services.settings, services.broker, None)


def test_state_totals_are_consistent(client, services):
    invest(client, services)
    d = state(client)
    t = {k: D(v) for k, v in d["totals"].items()}
    assert d["broker"]["mode"] == "simulated" and {h["ticker"] for h in d["holdings"]} == {"VTI", "VXUS"} and len(d["purchases"]) == 4
    assert t["total_value"] == t["positions_value"] + t["cash"]
    assert abs(t["profit_loss"] - (t["total_value"] - t["cashback_total"] + t["withdrawn"])) <= D("0.01")
    assert abs(sum(D(h["weight"]) for h in d["holdings"]) - 1) < D("0.001")
    assert D(d["pot"]["available"]) == t["cash"] and any("Bought" in a["description"] for a in d["activity"])
    assert d["latest_payout"] is None and "Not financial advice" not in d["disclaimer"] and "not financial advice" in d["disclaimer"]


def test_state_explains_why_cash_is_waiting(client):
    assert "Nothing waiting" in state(client)["status_message"]
    buy(client, "ym-flow")
    msg = state(client)["status_message"]
    assert msg.startswith("Held $") and "fee drag" in msg and D(state(client)["invest_threshold"]) == D("10.00")


def test_history_is_value_vs_invested_cost_with_a_live_point(client, services):
    assert client.get("/api/history").json() == []                              # nothing to chart yet
    buy(client, "sw-pulse")
    h = client.get("/api/history").json()
    assert len(h) >= 2 and set(h[-1]) == {"t", "value", "invested_cost"}
    assert D(h[-1]["invested_cost"]) == D("2.58") and D(h[-1]["value"]) == D("2.58")
    assert client.get("/api/history", params={"days": 7}).status_code == 200
    assert client.get("/api/history", params={"days": 0}).status_code == 422


def test_snapshots_are_throttled_and_skip_an_empty_account(session):
    assert ledger.record_snapshot(session, {}) is None
    ledger.credit_pot(session, D("5"), "cashback")
    assert ledger.record_snapshot(session, {}) is not None and ledger.record_snapshot(session, {}) is None
    assert ledger.record_snapshot(session, {}, force=True) is not None
    assert ledger.record_snapshot(session, {}, now=utcnow() + timedelta(seconds=11)) is not None


def test_every_executed_trade_writes_a_history_snapshot(client, services):
    buy(client, "sw-pulse")
    with services.session_factory() as s:
        before = len(s.scalars(select(PortfolioSnapshot)).all())
    invest(client, services, 3)
    with services.session_factory() as s:
        trades = len(s.scalars(select(TradeProposal).where(TradeProposal.status == "EXECUTED")).all())
        assert trades == 2 and len(s.scalars(select(PortfolioSnapshot)).all()) >= before + trades


# ------------------------------------------------------------------ demo seed
def seeded(services):
    with services.session_factory() as s:
        did = seed.seed_demo(s, services.broker, services.settings)
        s.commit()
    return did


def test_seed_writes_about_30_days_of_consistent_demo_history(services):
    assert seeded(services) is True
    with services.session_factory() as s:
        credits = s.scalars(select(CashbackCredit)).all()
        trades = s.scalars(select(TradeProposal)).all()
        snaps = s.scalars(select(PortfolioSnapshot).order_by(PortfolioSnapshot.created_at)).all()
        assert len(credits) == 40 and all(D("0.30") <= c.cashback_amount <= D("2.00") for c in credits)
        assert 5 <= len(trades) <= 10 and all(t.status == "EXECUTED" for t in trades)
        assert len(snaps) > 600 and (snaps[-1].created_at - snaps[0].created_at) > timedelta(days=25)
        assert ledger.available_balance(s) < D("10.00")                          # a realistic amount still waiting
        assert abs(sum(c.cashback_amount for c in credits) - ledger.total_cashback(s)) == 0
        spent = sum(t.dollar_amount for t in trades)
        assert abs(sum(p.quantity * p.avg_cost for p in ledger.get_positions(s)) - spent) < D("0.05")     # cost basis = money spent
        assert snaps[-1].invested_cost == ledger.total_cashback(s)                 # money put in = all cashback, nothing withdrawn
        texts = [e.description for e in s.scalars(select(LedgerEvent))]
        assert texts[0].startswith("Demo data loaded") and any(t.startswith("Bought $") for t in texts)
        assert any(t.startswith("Credited $") for t in texts) and any(t.startswith("Held $") for t in texts)


def test_seed_is_idempotent_and_never_overwrites_real_data(services):
    assert seeded(services) is True and seeded(services) is False
    with services.session_factory() as s:
        assert len(s.scalars(select(CashbackCredit)).all()) == 40


def test_seed_skips_a_database_that_already_has_activity(client, services):
    buy(client)
    assert seeded(services) is False


def test_seeded_history_joins_up_with_live_prices(client, services):
    seeded(services)
    h = client.get("/api/history").json()
    assert len(h) > 600 and h[-1]["t"] >= h[-2]["t"]
    assert abs(D(h[-1]["value"]) - D(h[-2]["value"])) < D("1.00")                # no cliff between the last stored point and "now"
    d = state(client)
    assert d["holdings"] and D(d["invest_threshold"]) - D(d["pot"]["available"]) > 0
    assert client.get("/api/health").json()["seeded"] is True


def test_seed_on_start_runs_through_the_app_factory(session_factory, fake_paypal):
    from app.api import Services
    from app.brokerage.sim_broker import SimBroker
    from app.main import create_app
    svc = Services(make_settings(seed_on_start=True), session_factory, SimBroker(), None, fake_paypal)
    c = TestClient(create_app(svc, enable_scheduler=False, frontend_dist=None))
    assert c.get("/api/health").json()["seeded"] is True and len(c.get("/api/products").json()) == 10


# ------------------------------------------------------------------ withdrawing investments
def test_withdrawing_more_than_cash_needs_the_include_investments_option(client, services):
    invest(client, services)
    over = D(state(client)["totals"]["cash"]) + D("1.00")
    r = client.post("/api/withdraw", json={"amount": str(over)})
    assert r.status_code == 422 and "include" in r.json()["detail"].lower()


def test_withdraw_sells_investments_pro_rata_then_pays_out(client, services):
    invest(client, services)
    t = state(client)["totals"]
    want = (D(t["positions_value"]) / 2).quantize(D("0.01"))
    r = client.post("/api/withdraw", json={"amount": str(want), "include_investments": True})
    assert r.status_code == 200, r.text
    assert "Sold investments" in r.json()["message"]
    after = state(client)
    assert D(after["totals"]["positions_value"]) < D(t["positions_value"]) and {h["ticker"] for h in after["holdings"]} == {"VTI", "VXUS"}
    assert after["latest_payout"]["status"] == "IN_TRANSIT" and D(after["latest_payout"]["amount"]) == want
    with services.session_factory() as s:
        sells = s.scalars(select(TradeProposal).where(TradeProposal.side == "sell")).all()
        assert sells and all(x.policy_reason == "WITHDRAWAL_SELL" for x in sells)


def test_withdraw_everything_leaves_no_dust(client, services):
    invest(client, services)
    r = client.post("/api/withdraw", json={"amount": state(client)["totals"]["max_withdrawable"], "include_investments": True})
    assert r.status_code == 200, r.text
    assert state(client)["holdings"] == []
    with services.session_factory() as s:
        assert ledger.get_positions(s) == [] and ledger.available_balance(s) == D("0.00")


def test_cannot_withdraw_more_than_everything_you_have(client, services):
    invest(client, services)
    r = client.post("/api/withdraw", json={"amount": str(D(state(client)["totals"]["max_withdrawable"]) + 5), "include_investments": True})
    assert r.status_code == 422 and "cannot be raised" in r.json()["detail"]


def test_stale_quotes_block_selling_instead_of_guessing(client, services):
    invest(client, services)
    services.broker.quote = lambda t: Quote(t, D("100"), datetime(2020, 1, 1))
    r = client.post("/api/withdraw", json={"amount": "3.00", "include_investments": True})
    assert r.status_code == 409 and "Rejected" in r.json()["detail"]


def test_withdrawal_sell_policy_is_pure_and_checks_quotes():
    from app.policy import validate_withdrawal_sell
    from app.schemas import VerdictStatus
    now = utcnow()
    fresh = LedgerSnapshot(now=now, pot_available=D("0"), quotes={"VTI": Quote("VTI", D("100"), now)})
    assert validate_withdrawal_sell("VTI", D("5"), fresh).status is VerdictStatus.APPROVED
    assert validate_withdrawal_sell("VTI", D("0"), fresh).code == "NON_POSITIVE_AMOUNT"
    assert validate_withdrawal_sell("VXUS", D("5"), fresh).code == "QUOTE_MISSING"
    old = LedgerSnapshot(now=now, pot_available=D("0"), quotes={"VTI": Quote("VTI", D("100"), now - timedelta(hours=1))})
    assert validate_withdrawal_sell("VTI", D("5"), old).code == "QUOTE_STALE"


def test_simulated_prices_move_like_a_market_but_deterministically():
    from app.brokerage.sim_broker import default_price_fn
    t0 = datetime(2026, 11, 9, 12, 0, 0)
    series = [default_price_fn("VTI", t0 + timedelta(seconds=10 * i)) for i in range(60)]
    assert len(set(series)) > 30 and max(series) / min(series) > D("1.005")
    assert series == [default_price_fn("VTI", t0 + timedelta(seconds=10 * i)) for i in range(60)]
    assert all(D("250") < p < D("330") for p in series)


# ------------------------------------------------------------------ deployment plumbing
def test_postgres_urls_are_normalised_for_sqlalchemy():
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert normalize_url("sqlite:///./penny.db") == "sqlite:///./penny.db"


def test_the_built_frontend_is_served_with_an_index_fallback_but_api_404s_stay_404(services, tmp_path):
    from app.main import create_app
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>PENNY APP</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    c = TestClient(create_app(services, enable_scheduler=False, frontend_dist=tmp_path))
    assert "PENNY APP" in c.get("/").text and "PENNY APP" in c.get("/some/client/route").text
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/api/does-not-exist").status_code == 404 and c.get("/api/health").json()["status"] == "ok"
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("TOP SECRET")
    assert "TOP SECRET" not in c.get("/..%2fsecret.txt").text and "TOP SECRET" not in c.get("/%2e%2e/secret.txt").text


def test_without_a_build_the_root_explains_how_to_build_the_frontend(client):
    assert "Frontend not built" in client.get("/").json()["message"]


def test_cors_allows_the_vite_dev_origin(client):
    r = client.options("/api/state", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
