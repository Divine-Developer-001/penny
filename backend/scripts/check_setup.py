"""Setup check: tries your .env credentials against PayPal sandbox, your LLM provider and Alpaca paper.

  python scripts/check_setup.py

It only reads (OAuth token, model list, account status) and never moves money or places orders.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402
from app.paypal.client import PayPalClient, PayPalError  # noqa: E402

OK, BAD, INFO = "[ok]  ", "[FAIL]", "[note]"


def check_paypal(s: Settings) -> list[tuple[str, str]]:
    if not s.paypal_configured:
        return [(BAD, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are missing or still placeholders")]
    out = []
    if "sandbox" not in s.paypal_api_base:
        out.append((BAD, f"PAYPAL_API_BASE is {s.paypal_api_base}; Penny is meant for the sandbox only"))
    try:
        PayPalClient(s).get_token()
        out.append((OK, "PayPal OAuth works (credentials are valid)"))
    except (PayPalError, httpx.HTTPError) as exc:
        out.append((BAD, f"PayPal OAuth failed: {exc}. Use the SANDBOX app's client id/secret."))
    if s.paypal_webhook_id == "WEBHOOK_ID":
        out.append((INFO, "PAYPAL_WEBHOOK_ID=WEBHOOK_ID -> simulator mode (fine for the simulator; use the real id for real events)"))
    else:
        out.append((OK, f"PAYPAL_WEBHOOK_ID set ({s.paypal_webhook_id[:6]}...)"))
    if s.paypal_payout_receiver_email.endswith("@example.com"):
        out.append((BAD, "PAYPAL_PAYOUT_RECEIVER_EMAIL is still the example address; use a sandbox personal account email"))
    return out


def check_llm(s: Settings) -> list[tuple[str, str]]:
    if not s.llm_configured:
        return [(INFO, "LLM_API_KEY not set -> Penny will use its rule-based planner")]
    base, headers = s.llm_base_url.rstrip("/"), {"Authorization": f"Bearer {s.llm_api_key}"}
    out = []
    try:
        r = httpx.get(f"{base}/models", headers=headers, timeout=20)
        if r.status_code == 401:
            return [(BAD, "LLM key rejected (HTTP 401). Check LLM_API_KEY and LLM_BASE_URL belong to the same provider")]
        r.raise_for_status()
        ids = {m.get("id") for m in r.json().get("data", [])}
        if s.llm_model in ids:
            out.append((OK, f"LLM key works and model '{s.llm_model}' exists"))
        else:
            sample = ", ".join(sorted(i for i in ids if i)[:8])
            return [(BAD, f"model '{s.llm_model}' not found. Set LLM_MODEL to one of: {sample} ...")]
        probe = {"model": s.llm_model, "temperature": 0, "tool_choice": "required",
                 "messages": [{"role": "user", "content": "Call the ping tool."}],
                 "tools": [{"type": "function", "function": {"name": "ping", "description": "ping",
                            "parameters": {"type": "object", "properties": {}}}}]}
        r = httpx.post(f"{base}/chat/completions", headers=headers, json=probe, timeout=30)
        r.raise_for_status()
        if r.json()["choices"][0]["message"].get("tool_calls"):
            out.append((OK, "model supports tool calling (required by Penny's agent)"))
        else:
            out.append((BAD, "model answered without calling the tool; pick a model that supports tool calling"))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        out.append((BAD, f"LLM check failed: {exc}"))
    return out


def check_alpaca(s: Settings) -> list[tuple[str, str]]:
    if s.broker_mode.lower() != "alpaca":
        return [(INFO, "BROKER_MODE=simulated -> built-in simulated broker")]
    if not s.alpaca_configured:
        return [(INFO, "Alpaca keys not set -> Penny will use the simulated broker")]
    try:
        from app.brokerage.alpaca_broker import AlpacaBroker
        broker = AlpacaBroker(s.alpaca_api_key_id, s.alpaca_secret_key)
        acct = broker._trading.get_account()
        out = [(OK, f"Alpaca PAPER account reachable (status {acct.status})")]
        from app.schemas import utcnow
        age = int((utcnow() - broker.quote("VTI").as_of).total_seconds())
        out.append((OK, f"VTI quote is {age}s old") if age <= s.quote_staleness_seconds else
                   (INFO, f"VTI quote is {age}s old (> {s.quote_staleness_seconds}s): US market is probably closed, "
                          "so Penny will reject orders now. Demo 9:30-16:00 US Eastern, or set BROKER_MODE=simulated"))
        return out
    except Exception as exc:                                  # noqa: BLE001
        return [(BAD, f"Alpaca check failed: {exc}. Use PAPER keys from a paper-only account")]


def check_channel3(s: Settings) -> list[tuple[str, str]]:
    if not s.channel3_api_key.strip():
        return [(INFO, "CHANNEL3_API_KEY not set -> the store shows labelled sample deals")]
    try:
        r = httpx.post("https://api.trychannel3.com/v1/search", headers={"x-api-key": s.channel3_api_key.strip()},
                       json={"query": "running shoes"}, timeout=20)
        if r.status_code in (401, 403):
            return [(BAD, f"Channel3 rejected the key (HTTP {r.status_code}). Check CHANNEL3_API_KEY")]
        r.raise_for_status()
        n = sum(len(p.get("offers") or []) for p in r.json().get("products") or [])
        return [(OK, f"Channel3 works ({n} offers found for a test search)")] if n else [(INFO, "Channel3 key works but the test search returned no offers")]
    except (httpx.HTTPError, ValueError) as exc:
        return [(BAD, f"Channel3 check failed: {exc}")]


def main() -> int:
    s = Settings()
    failed = False
    for title, fn in (("PayPal sandbox", check_paypal), ("LLM", check_llm), ("Broker", check_alpaca), ("Channel3 (deals)", check_channel3)):
        print(f"\n{title}")
        for tag, msg in fn(s):
            print(f"  {tag} {msg}")
            failed |= tag == BAD
    print("\nAll good." if not failed else "\nFix the [FAIL] items above, then run again.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
