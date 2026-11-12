"""End-to-end through the API with fakes: webhook -> pot -> agent -> policy gate -> ledger -> payout."""
import json
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import select

from app import ledger
from app.agent.loop import run_agent
from app.brokerage.sim_broker import SimBroker
from app.models import LedgerEvent, Payout, Position, ProposalStatus, TradeProposal
from app.paypal import payouts
from app.schemas import utcnow
from tests.conftest import FakeLLM, assistant, make_settings, tool_call


def capture_event(event_id="WH-1", amount="21.00"):
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource_type": "capture",
            "resource": {"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": amount}}}


def post(client, event, tid="T-1"):
    return client.post("/webhooks/paypal", content=json.dumps(event),
                       headers={"PAYPAL-TRANSMISSION-ID": tid, "Content-Type": "application/json"})


def pot(client):
    return {k: D(v) for k, v in client.get("/api/state").json()["pot"].items()}


def fund(services, amount):
    with services.session_factory() as s:
        ledger.credit_pot(s, D(amount), "test")
        s.commit()


# ------------------------------------------------------------------ webhooks
def test_webhook_credits_the_pot_and_is_idempotent(client):
    r = post(client, capture_event())
    assert r.status_code == 200 and r.json()["detail"] == "credited"
    assert pot(client)["available"] == D("0.42")
    again = post(client, capture_event())                                  # PayPal retry: same event, same transmission
    assert again.json()["status"] == "duplicate"
    assert pot(client)["available"] == D("0.42")


def test_unknown_events_are_acknowledged_not_errors(client):
    r = post(client, {"id": "WH-X", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {}})
    assert r.status_code == 200 and r.json()["detail"] == "unhandled"


def test_garbage_body_is_a_400(client):
    assert client.post("/webhooks/paypal", content="not json").status_code == 400


def test_simulator_webhook_id_is_refused_outside_the_sandbox(client, services):
    services.settings.paypal_env = "live"
    assert post(client, capture_event()).status_code == 401
    assert pot(client)["available"] == D("0")


def test_non_usd_capture_is_ignored(client):
    event = capture_event()
    event["resource"]["amount"]["currency_code"] = "EUR"
    assert post(client, event).json()["detail"] == "ignored"
    assert pot(client)["available"] == D("0")


# ------------------------------------------------------------------ agent: rule-based path
def test_agent_holds_when_below_threshold(client, services):
    fund(services, "6.15")
    r = client.post("/api/agent/run").json()
    assert r["mode"] == "rule_based" and r["proposal_ids"] == []
    assert r["explanation"].startswith("Held $6.15 in your pot")
    assert pot(client)["available"] == D("6.15")


def test_agent_skips_an_empty_pot(client):
    assert client.post("/api/agent/run").json()["skipped"] == "empty pot"


def test_agent_invests_through_the_policy_gate(client, services):
    fund(services, "12")
    r = client.post("/api/agent/run").json()
    assert len(r["proposal_ids"]) == 2
    state = client.get("/api/state").json()
    assert {p["ticker"] for p in state["holdings"]} == {"VTI", "VXUS"}
    assert pot(client)["available"] == D("0.00")
    texts = [e["description"] for e in client.get("/api/explain").json()]
    assert any(t.startswith("Bought $9.60 of VTI") for t in texts)
    assert any("band 70–90%" in t for t in texts)
    bought = [t for t in texts if t.startswith("Bought")]
    assert len(bought) == 2 and all("from 0% to 80%" in t for t in bought)      # a basket is explained as a whole
    assert "not financial advice" in state["disclaimer"]


def test_undo_window_defers_execution_and_undo_cancels(session_factory, fake_paypal):
    from app.api import Services
    from app.main import create_app
    from fastapi.testclient import TestClient
    settings = make_settings(undo_window_seconds=60)
    svc = Services(settings, session_factory, SimBroker(), None, fake_paypal)
    c = TestClient(create_app(svc, enable_scheduler=False))
    fund(svc, "12")
    ids = c.post("/api/agent/run").json()["proposal_ids"]
    state = c.get("/api/state").json()
    assert state["holdings"] == [] and D(state["pot"]["committed"]) == D("12.00")      # reserved, not spent
    assert D(state["pot"]["uncommitted"]) == D("0")

    assert c.post(f"/api/proposals/{ids[0]}/undo").json()["proposal"]["status"] == "UNDONE"
    with svc.session_factory() as s:                                                    # time travels past the window
        done = ledger.process_due(s, svc.broker, ledger.get_profile(s, settings), utcnow() + timedelta(seconds=120))
        s.commit()
        assert [p.id for p in done] == [ids[1]]
        assert {p.ticker for p in ledger.get_positions(s)} == {"VXUS"}
        assert ledger.available_balance(s) == D("9.60")                                 # undone order's cash stays in the pot
    assert c.post(f"/api/proposals/{ids[1]}/undo").status_code == 409                       # already executed


def test_undo_after_the_deadline_is_409(session_factory, fake_paypal):
    from app.api import Services
    from app.main import create_app
    from fastapi.testclient import TestClient
    svc = Services(make_settings(undo_window_seconds=60), session_factory, SimBroker(), None, fake_paypal)
    c = TestClient(create_app(svc, enable_scheduler=False))
    fund(svc, "12")
    pid = c.post("/api/agent/run").json()["proposal_ids"][0]
    with svc.session_factory() as s:
        s.get(TradeProposal, pid).undo_deadline = utcnow() - timedelta(seconds=1)
        s.commit()
    assert c.post(f"/api/proposals/{pid}/undo").status_code == 409


def test_over_cap_order_waits_for_human_approval(client, services):
    fund(services, "60")
    ids = client.post("/api/agent/run").json()["proposal_ids"]
    state = client.get("/api/state").json()
    assert [p["ticker"] for p in state["pending_approvals"]] == ["VTI"]                # $48 > $25 cap
    assert {p["ticker"] for p in state["holdings"]} == {"VXUS"}                       # the $12 leg auto-executed
    vti = state["pending_approvals"][0]["id"]
    assert vti in ids
    assert client.post(f"/api/proposals/{vti}/approve").status_code == 200
    final = client.get("/api/state").json()
    assert {p["ticker"] for p in final["holdings"]} == {"VTI", "VXUS"} and final["pending_approvals"] == []
    assert client.post(f"/api/proposals/{vti}/approve").status_code == 409                 # not pending any more


def test_user_can_reject_a_pending_order(client, services):
    fund(services, "60")
    client.post("/api/agent/run")
    vti = client.get("/api/state").json()["pending_approvals"][0]["id"]
    assert client.post(f"/api/proposals/{vti}/reject").json()["proposal"]["status"] == "REJECTED"
    assert D(client.get("/api/state").json()["pot"]["available"]) == D("48.00")


def test_unknown_proposal_is_404(client):
    assert client.post("/api/proposals/999/approve").status_code == 404


def test_profile_switch_changes_the_allowlist(client, services):
    assert client.post("/api/profile", json={"profile": "conservative"}).json() == {"profile": "conservative"}
    assert client.post("/api/profile", json={"profile": "yolo"}).status_code == 422
    fund(services, "10")
    client.post("/api/agent/run")
    assert {p["ticker"] for p in client.get("/api/state").json()["holdings"]} == {"VTI", "BND"}


# ------------------------------------------------------------------ agent: LLM path
def run_llm(services, script):
    llm = FakeLLM(script)
    with services.session_factory() as s:
        return run_agent(s, services.settings, services.broker, llm), llm


def test_llm_proposals_are_executed_only_via_tools_and_policy(services):
    fund(services, "12")
    result, llm = run_llm(services, [
        assistant(tool_call("get_threshold_decision", {})),
        assistant(tool_call("propose_order", {"ticker": "VTI", "dollar_amount": 9.6, "rationale": "underweight"}, "a"),
                  tool_call("propose_order", {"ticker": "VXUS", "dollar_amount": 2.4, "rationale": "underweight"}, "b")),
        assistant(tool_call("explain", {"text": "Invested $12.00: $9.60 in VTI and $2.40 in VXUS."})),
    ])
    assert result["mode"] == "llm" and result["iterations"] == 3 and len(result["proposal_ids"]) == 2
    assert result["explanation"] == "Invested $12.00: $9.60 in VTI and $2.40 in VXUS."
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["verdict"] == "APPROVED"
    with services.session_factory() as s:
        assert {p.ticker for p in ledger.get_positions(s)} == {"VTI", "VXUS"}


def test_policy_blocks_an_llm_that_proposes_a_forbidden_ticker(services):
    fund(services, "12")
    result, llm = run_llm(services, [
        assistant(tool_call("propose_order", {"ticker": "TSLA", "dollar_amount": 12, "rationale": "yolo"})),
        assistant(tool_call("explain", {"text": "Tried TSLA."})),
    ])
    with services.session_factory() as s:
        assert ledger.get_positions(s) == [] and ledger.available_balance(s) == D("12")
        row = s.scalars(select(TradeProposal)).one()
        assert row.status == ProposalStatus.REJECTED and row.policy_reason == "TICKER_NOT_ALLOWED"
        assert any("TSLA isn't on your growth allowlist" in e.description for e in s.scalars(select(LedgerEvent)))
    assert json.loads([m for m in llm.seen[-1] if m["role"] == "tool"][-1]["content"])["verdict"] == "REJECTED"


def test_llm_cannot_widen_an_order_beyond_the_plan(services):
    fund(services, "12")
    run_llm(services, [
        assistant(tool_call("get_threshold_decision", {})),
        assistant(tool_call("propose_order", {"ticker": "VTI", "dollar_amount": 12, "rationale": "all in"})),
        assistant(tool_call("explain", {"text": "Done."})),
    ])
    with services.session_factory() as s:
        rows = {r.ticker: r for r in s.scalars(select(TradeProposal))}
        assert rows["VTI"].status == ProposalStatus.REJECTED          # $12 of VTI alone breaks the pot/plan rules
        assert ledger.get_positions(s) == [] or {p.ticker for p in ledger.get_positions(s)} <= {"VXUS"}
        assert ledger.available_balance(s) >= D("9.60")               # the big VTI order never executed


def test_a_mistyped_ticker_is_rejected_and_the_planner_completes_the_plan(services):
    """gpt-oss wrote VXAT for VXUS: policy rejects it, the planner still buys the planned VXUS leg."""
    fund(services, "12")
    result, _ = run_llm(services, [
        assistant(tool_call("get_threshold_decision", {})),
        assistant(tool_call("propose_order", {"ticker": "VTI", "dollar_amount": 9.6, "rationale": "x"}, "a"),
                  tool_call("propose_order", {"ticker": "VXAT", "dollar_amount": 2.4, "rationale": "typo"}, "b")),
        assistant(tool_call("explain", {"text": "Invested $9.60 in VTI (VXUS order was rejected)."})),
    ])
    assert result["mode"] == "llm+planner" and len(result["proposal_ids"]) == 3
    assert result["explanation"] == "Proposed 3 orders totalling $12.00: 2 approved, 0 awaiting you, 1 rejected by policy."
    with services.session_factory() as s:
        statuses = {(r.ticker, r.status) for r in s.scalars(select(TradeProposal))}
        assert statuses == {("VTI", "EXECUTED"), ("VXAT", "REJECTED"), ("VXUS", "EXECUTED")}
        assert ledger.available_balance(s) == D("0.00")
        assert any("VXAT isn't on your growth allowlist" in e.description for e in s.scalars(select(LedgerEvent)))


def test_ungrounded_llm_explanations_are_replaced_by_template_text(services):
    fund(services, "6.15")
    result, _ = run_llm(services, [
        assistant(tool_call("get_threshold_decision", {})),
        assistant(tool_call("explain", {"text": "Your savings will grow 37% this year!"})),
    ])
    assert result["explanation"].startswith("Held $6.15 in your pot")
    with services.session_factory() as s:
        last = s.scalars(select(LedgerEvent).where(LedgerEvent.kind == "agent_summary")).one()
        assert last.payload["grounded"] is False and "37" not in last.description


def test_bad_tool_arguments_become_tool_errors_not_crashes(services):
    fund(services, "12")
    result, llm = run_llm(services, [
        assistant(tool_call("propose_order", "{not json")),
        assistant(tool_call("nonexistent_tool", {})),
        assistant(tool_call("explain", {"text": "Nothing done."})),
    ])
    contents = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert "not valid JSON" in contents[0] and "unknown tool" in contents[1]


def test_llm_failure_falls_back_to_the_rule_based_planner(services):
    fund(services, "12")
    result, _ = run_llm(services, [RuntimeError("provider down")])
    assert result["mode"] == "rule_based" and len(result["proposal_ids"]) == 2
    assert result["llm_error"] == "provider down"                         # the API tells you *why*


# ------------------------------------------------------------------ payouts
def test_withdraw_moves_cash_in_transit_then_webhook_completes_it(client, services, fake_paypal):
    fund(services, "8")
    r = client.post("/api/withdraw", json={"amount": "5.00"})
    assert r.status_code == 200 and r.json()["status"] == "IN_TRANSIT"
    assert pot(client)["available"] == D("3.00") and pot(client)["in_transit"] == D("5.00")
    body = next(b for m, p, b in fake_paypal.calls if p == "/v1/payments/payouts")
    assert body["items"][0]["note"] != "ERRPYO002" and body["items"][0]["amount"] == {"value": "5.00", "currency": "USD"}

    event = {"id": "WH-P1", "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
             "resource": {"payout_item_id": "ITEM1", "payout_batch_id": "BATCH1", "transaction_status": "SUCCESS"}}
    assert post(client, event, "T-P1").json()["detail"] == "payout_completed"
    assert pot(client)["in_transit"] == D("0") and pot(client)["available"] == D("3.00")
    with services.session_factory() as s:
        assert s.scalars(select(Payout)).one().status == "COMPLETED"


def test_failed_payout_webhook_restores_the_cash(client, services):
    fund(services, "8")
    client.post("/api/withdraw", json={"amount": "5.00"})
    event = {"id": "WH-P2", "event_type": "PAYMENT.PAYOUTS-ITEM.FAILED",
             "resource": {"payout_item_id": "ITEM1", "errors": {"name": "SENDER_EMAIL_UNCONFIRMED"}}}
    assert post(client, event, "T-P2").json()["detail"] == "payout_failed"
    assert pot(client)["available"] == D("8.00") and pot(client)["in_transit"] == D("0")
    texts = [e["description"] for e in client.get("/api/explain").json()]
    assert any("SENDER_EMAIL_UNCONFIRMED" in t and "back in your pot" in t for t in texts)


def test_withdraw_more_than_the_pot_is_refused(client, services):
    fund(services, "3")
    assert client.post("/api/withdraw", json={"amount": "5.00"}).status_code == 422
    assert pot(client)["available"] == D("3.00")


def test_withdraw_cannot_spend_cash_committed_to_a_pending_order(client, services):
    fund(services, "60")
    client.post("/api/agent/run")                                   # $48 VTI now waits for approval
    assert client.post("/api/withdraw", json={"amount": "20.00"}).status_code == 422


def test_paypal_outage_during_withdraw_restores_cash(client, services, fake_paypal):
    fund(services, "8")
    fake_paypal.fail_create = True
    assert client.post("/api/withdraw", json={"amount": "5.00"}).status_code == 502
    assert pot(client)["available"] == D("8.00") and pot(client)["in_transit"] == D("0")


def test_polling_settles_a_payout_without_a_webhook(client, services, fake_paypal):
    fund(services, "8")
    client.post("/api/withdraw", json={"amount": "5.00"})
    fake_paypal.item_status = "SUCCESS"
    with services.session_factory() as s:
        assert payouts.poll_payouts(s, fake_paypal) == 1
        s.commit()
    assert pot(client)["in_transit"] == D("0")


def test_negative_test_flag_injects_errpyo002(services, fake_paypal):
    s = make_settings(paypal_negative_test=True)
    payouts.create_payout(fake_paypal, s, D("5"))
    body = next(b for m, p, b in fake_paypal.calls if p == "/v1/payments/payouts")
    assert body["items"][0]["note"] == "ERRPYO002"


def test_item_status_mapping():
    assert payouts.map_item_status("SUCCESS") == "COMPLETED"
    assert payouts.map_item_status("PENDING") == "IN_TRANSIT"
    assert payouts.map_item_status("ONHOLD") == "IN_TRANSIT"
    assert payouts.map_item_status("FAILED") == "FAILED" and payouts.map_item_status("RETURNED") == "FAILED"


def test_basket_explanation_survives_price_drift_between_legs(services):
    """Sim prices move between minutes; leg 2 must still read 'from 0% to 80%', not a dust-driven 100%."""
    import itertools
    from app.brokerage.sim_broker import SimBroker
    tick = itertools.count()
    services.broker = SimBroker(price_fn=lambda t, now: D("100") + D(next(tick)) / D("1000"))
    fund(services, "18")
    with services.session_factory() as s:
        run_agent(s, services.settings, services.broker, None)
        bought = [e.description for e in s.scalars(select(LedgerEvent).where(LedgerEvent.kind == "trade_executed"))]
    assert len(bought) == 2 and all("from 0% to 80%" in b for b in bought), bought


class _Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, text

    def json(self):
        return self._body


class _Http:
    def __init__(self, *responses):
        self.responses, self.posts = list(responses), 0

    def post(self, *a, **k):
        self.posts += 1
        return self.responses.pop(0)


def test_llm_client_retries_a_short_rate_limit_then_succeeds():
    from app.agent.loop import OpenAICompatClient
    ok = _Resp(200, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    http, slept = _Http(_Resp(429, headers={"retry-after": "3"}, text="slow down"), ok), []
    msg = OpenAICompatClient(make_settings(llm_api_key="k"), http, sleep=slept.append).chat([], [])
    assert msg["content"] == "hi" and http.posts == 2 and slept == [3.5]


def test_llm_client_does_not_wait_for_long_rate_limits_and_reports_the_provider_message():
    import pytest
    from app.agent.loop import OpenAICompatClient
    http = _Http(_Resp(429, headers={"retry-after": "600"}, text="Rate limit reached, try again in 10m"))
    with pytest.raises(RuntimeError, match="LLM HTTP 429: Rate limit reached"):
        OpenAICompatClient(make_settings(llm_api_key="k"), http, sleep=lambda s: None).chat([], [])
    assert http.posts == 1


def test_llm_client_surfaces_bad_model_errors():
    import pytest
    from app.agent.loop import OpenAICompatClient
    http = _Http(_Resp(404, text='{"error":{"message":"The model `nope` does not exist"}}'))
    with pytest.raises(RuntimeError, match="HTTP 404.*does not exist"):
        OpenAICompatClient(make_settings(llm_api_key="k"), http).chat([], [])


GROQ_COMMENTARY_400 = json.dumps({"error": {
    "message": "Tool call validation failed: attempted to call tool 'commentary' which was not in request.tools",
    "type": "invalid_request_error", "code": "tool_use_failed",
    "failed_generation": json.dumps({"name": "commentary", "arguments": {"text": "Invested the full $12.00."}})}})


def test_client_turns_a_rejected_commentary_call_into_plain_text():
    from app.agent.loop import OpenAICompatClient
    from app.agent.tools import TOOL_SCHEMAS
    http = _Http(_Resp(400, text=GROQ_COMMENTARY_400))
    msg = OpenAICompatClient(make_settings(llm_api_key="k"), http).chat([], TOOL_SCHEMAS)
    assert msg == {"role": "assistant", "content": "Invested the full $12.00."} and http.posts == 1


def test_client_replays_a_rejected_call_that_names_a_real_tool():
    from app.agent.loop import OpenAICompatClient
    from app.agent.tools import TOOL_SCHEMAS
    body = json.dumps({"error": {"code": "tool_use_failed", "failed_generation":
                                 json.dumps({"name": "get_pending_balance", "arguments": {}})}})
    msg = OpenAICompatClient(make_settings(llm_api_key="k"), _Http(_Resp(400, text=body))).chat([], TOOL_SCHEMAS)
    assert msg["tool_calls"][0]["function"]["name"] == "get_pending_balance"


def test_client_retries_unrecoverable_tool_failures_then_gives_up():
    import pytest
    from app.agent.loop import OpenAICompatClient
    bad = json.dumps({"error": {"code": "tool_use_failed", "failed_generation": "garbage"}})
    http = _Http(_Resp(400, text=bad), _Resp(400, text=bad), _Resp(400, text=bad))
    with pytest.raises(RuntimeError, match="tool_use_failed"):
        OpenAICompatClient(make_settings(llm_api_key="k"), http).chat([], [])
    assert http.posts == 3


def test_a_model_that_only_talks_does_not_leave_cash_uninvested(services):
    """gpt-oss said 'Invested the full $12.00' without calling a single tool."""
    fund(services, "12")
    result, _ = run_llm(services, [{"role": "assistant", "content": "Invested the full $12.00."}])
    assert result["mode"] == "llm+planner" and len(result["proposal_ids"]) == 2
    assert "completed the plan" in result["llm_error"]
    with services.session_factory() as s:
        assert {p.ticker for p in ledger.get_positions(s)} == {"VTI", "VXUS"}
    assert result["explanation"].startswith("Proposed 2 orders totalling $12.00")


def test_a_model_that_only_talks_while_holding_gets_an_honest_hold_explanation(services):
    fund(services, "6.15")
    result, _ = run_llm(services, [{"role": "assistant", "content": "Invested the full $6.15."}])
    assert result["proposal_ids"] == [] and result["explanation"].startswith("Held $6.15 in your pot")


def test_llm_client_backs_off_exponentially_then_gives_up():
    import pytest
    from app.agent.loop import OpenAICompatClient
    ok = _Resp(200, {"choices": [{"message": {"role": "assistant", "content": "finally"}}]})
    http, slept = _Http(_Resp(429), _Resp(429), _Resp(429), ok), []
    assert OpenAICompatClient(make_settings(llm_api_key="k"), http, sleep=slept.append).chat([], [])["content"] == "finally"
    assert slept == [1.0, 2.0, 4.0] and http.posts == 4
    http = _Http(*[_Resp(429, text="busy") for _ in range(5)])
    with pytest.raises(RuntimeError, match="LLM HTTP 429"):
        OpenAICompatClient(make_settings(llm_api_key="k"), http, sleep=lambda s: None).chat([], [])
    assert http.posts == 4                                              # first try + 3 retries


def test_retry_after_larger_than_the_backoff_wins():
    from app.agent.loop import OpenAICompatClient
    ok = _Resp(200, {"choices": [{"message": {"role": "assistant", "content": "x"}}]})
    http, slept = _Http(_Resp(429, headers={"retry-after": "9"}), ok), []
    OpenAICompatClient(make_settings(llm_api_key="k"), http, sleep=slept.append).chat([], [])
    assert slept == [9.5]
