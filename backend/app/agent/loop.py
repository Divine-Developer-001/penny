"""Agent layer: the LLM tool-calling loop. The model proposes; tools -> policy gate -> ledger decide.

If the LLM is not configured or errors, a rule-based planner runs the *same tools* (so the policy
gate still applies) and the demo keeps working.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from app import ledger
from app.agent import tools
from app.agent.prompts import USER_KICKOFF, build_system_prompt
from app.brokerage.base import Broker
from app.config import Settings
from app.schemas import Rules, utcnow

logger = logging.getLogger(__name__)
MAX_ITERATIONS = 8
MAX_RATE_LIMIT_RETRIES = 3
MAX_BACKOFF_SECONDS = 30.0


class LLMClient(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", "")) + 0.5
    except ValueError:
        return None


def _recover_failed_generation(body_text: str, tool_names: set[str]) -> dict[str, Any] | None:
    """Turn a provider 'tool_use_failed' (HTTP 400) into a normal assistant message, if possible.

    gpt-oss models on Groq sometimes 'speak' by calling a non-existent tool such as `commentary`.
    If the rejected call names a real tool we replay it as a proper tool call; otherwise its text
    becomes a plain assistant answer (which Penny still grounding-checks before storing).
    """
    try:
        err = json.loads(body_text)["error"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(err, dict) or err.get("code") != "tool_use_failed":
        return None
    raw = err.get("failed_generation") or ""
    try:
        call = json.loads(raw)
    except ValueError:
        m_name = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
        m_text = re.search(r'"text"\s*:\s*"(.*?)"\s*\}', raw, re.S)
        if not m_name:
            return None
        call = {"name": m_name.group(1), "arguments": {"text": m_text.group(1)} if m_text else {}}
    if not isinstance(call, dict):
        return None
    name, args = call.get("name"), call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if name in tool_names:
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"recovered-{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}
    text = args.get("text") if isinstance(args, dict) else None
    return {"role": "assistant", "content": str(text)} if text else None


class OpenAICompatClient:
    """Chat-completions client for any OpenAI-compatible provider (configurable base URL).

    Errors carry the provider's own message; 429s are retried with exponential backoff (honouring
    Retry-After); and malformed tool calls (HTTP 400 tool_use_failed) are recovered or retried.
    """

    def __init__(self, settings: Settings, http: httpx.Client | None = None, sleep=time.sleep) -> None:
        self._s = settings
        self._http = http or httpx.Client(timeout=60.0)
        self._sleep = sleep

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{self._s.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._s.llm_api_key}", "Content-Type": "application/json"}
        payload = {"model": self._s.llm_model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "temperature": 0.2}
        names = {t["function"]["name"] for t in tools}
        rate_retries = tool_retries = 0
        while True:
            resp = self._http.post(url, headers=headers, json=payload)
            if resp.status_code == 429 and rate_retries < MAX_RATE_LIMIT_RETRIES:
                hinted = _retry_after(resp)
                if hinted is None or hinted <= MAX_BACKOFF_SECONDS:        # "try again in 10 minutes" is not worth waiting for
                    wait = max(float(2 ** rate_retries), hinted or 0.0)    # exponential: 1s, 2s, 4s (or Retry-After if larger)
                    rate_retries += 1
                    logger.info("LLM rate limited; retry %d/%d in %.1fs", rate_retries, MAX_RATE_LIMIT_RETRIES, wait)
                    self._sleep(wait)
                    continue
            if resp.status_code == 400 and "tool_use_failed" in resp.text:
                recovered = _recover_failed_generation(resp.text, names)
                if recovered is not None:
                    logger.info("recovered a rejected tool call from the provider's failed_generation")
                    return recovered
                if tool_retries < 2:
                    tool_retries += 1
                    logger.info("malformed tool call from the model; retrying")
                    continue
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()["choices"][0]["message"]


def make_llm(settings: Settings) -> LLMClient | None:
    return OpenAICompatClient(settings) if settings.llm_configured else None


def _rule_based(ctx: tools.ToolContext) -> int:
    """Same tools, no LLM: ask the threshold, propose each leg, explain from templates."""
    decision = tools.dispatch(ctx, "get_threshold_decision", {})
    if decision.get("invest"):
        for order in decision["orders"]:
            tools.dispatch(ctx, "propose_order", {"ticker": order["ticker"], "dollar_amount": order["dollar_amount"],
                                                  "rationale": "Rule-based planner (LLM unavailable)."})
    tools.dispatch(ctx, "explain", {"text": ""})          # empty text -> grounded template summary
    return 1


def _llm_loop(ctx: tools.ToolContext, llm: LLMClient) -> int:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(ctx.profile, ctx.rules)},
        {"role": "user", "content": USER_KICKOFF}]
    for i in range(1, MAX_ITERATIONS + 1):
        msg = llm.chat(messages, tools.TOOL_SCHEMAS)
        calls = msg.get("tool_calls") or []
        if not calls:                                       # model answered in prose: treat as its explanation
            if ctx.explanation is None:
                tools.dispatch(ctx, "explain", {"text": msg.get("content") or ""})
            return i
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            result = tools.dispatch(ctx, fn.get("name", ""), args) if args is not None \
                else {"error": "arguments were not valid JSON"}
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result)})
        if ctx.explanation is not None:                     # explain() is the last act
            return i
    if ctx.explanation is None:
        tools.dispatch(ctx, "explain", {"text": ""})
    return MAX_ITERATIONS


def _finish_plan(ctx: tools.ToolContext) -> int:
    """Propose any planned order the model never proposed (it skipped it or mistyped the ticker).

    A model that only talks, or that writes 'VXAT' for 'VXUS', must not leave part of the plan undone.
    The planner uses the same tools, so every order still goes through the policy gate. If the model
    proposed orders without ever consulting the threshold we don't know the plan, so we leave it alone.
    Returns how many things the planner did.
    """
    done = 0
    if ctx.decision is None and not ctx.proposal_ids:
        tools.dispatch(ctx, "get_threshold_decision", {})
        done += 1
    if ctx.decision is not None and ctx.decision.invest:
        for leg in list(ctx.plan):                          # legs still here were never proposed by ticker
            tools.dispatch(ctx, "propose_order", {"ticker": leg.ticker, "dollar_amount": leg.dollar_amount,
                                                  "rationale": "Rule-based planner (the model did not propose this order)."})
            done += 1
    if done:
        tools.dispatch(ctx, "explain", {"text": ""})          # refresh the explanation from real facts
    return done


def run_agent(session: Session, settings: Settings, broker: Broker, llm: LLMClient | None, now=None) -> dict[str, Any]:
    """Run one agent pass and commit. Returns a small summary for the API / demos."""
    now = now or utcnow()
    rules = Rules.from_settings(settings)
    profile = ledger.get_profile(session, settings)
    if ledger.uncommitted_pot(session) <= 0:
        return {"mode": "skipped", "iterations": 0, "proposal_ids": [], "explanation": "", "skipped": "empty pot", "llm_error": None}
    ctx = tools.ToolContext(session=session, settings=settings, rules=rules, broker=broker, profile=profile, now=now)
    mode, iterations, llm_error = "llm", 0, None
    try:
        if llm is None:
            raise RuntimeError("LLM not configured")
        iterations = _llm_loop(ctx, llm)
        if _finish_plan(ctx):
            mode, llm_error = "llm+planner", "the model left planned orders unproposed; the rule-based planner completed the plan"
    except Exception as exc:                               # noqa: BLE001
        logger.warning("LLM path unavailable (%s); using the rule-based planner", exc)
        llm_error = str(exc)[:400]
        if ctx.explanation is None:
            mode = "rule_based"
            iterations = _rule_based(ctx)
    if rules.undo_window_seconds <= 0:                       # no undo window: execute the whole batch together
        ledger.process_due(session, broker, profile, now)
    session.commit()
    return {"mode": mode, "iterations": iterations, "proposal_ids": ctx.proposal_ids,
            "explanation": ctx.explanation or "", "skipped": None, "llm_error": llm_error}
