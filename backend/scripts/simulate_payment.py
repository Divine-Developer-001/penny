"""Demo script (money-in): fire a PAYMENT.CAPTURE.COMPLETED event so Penny credits simulated cashback.

  python scripts/simulate_payment.py --amount 21.00
      posts a locally built event to the running app (needs PAYPAL_WEBHOOK_ID=WEBHOOK_ID, sandbox)
  python scripts/simulate_payment.py --via-paypal --target-url https://<ngrok-id>.ngrok.app/webhooks/paypal
      asks PayPal's simulate-event API to deliver a mock event to your public URL. PayPal's mock
      payload carries its own sample amount, so --amount is ignored in this mode.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.paypal.client import PayPalClient  # noqa: E402


def local_event(amount: Decimal) -> dict:
    return {
        "id": f"WH-{uuid.uuid4().hex[:17].upper()}",
        "create_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resource_type": "capture",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "summary": f"Payment completed for $ {amount} USD",
        "resource": {"id": uuid.uuid4().hex[:17].upper(), "status": "COMPLETED",
                     "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--amount", type=Decimal, default=Decimal("21.00"), help="purchase amount in USD (local mode)")
    ap.add_argument("--url", default="http://localhost:8000/webhooks/paypal", help="Penny webhook URL (local mode)")
    ap.add_argument("--via-paypal", action="store_true", help="use PayPal's POST /v1/notifications/simulate-event")
    ap.add_argument("--target-url", help="public HTTPS webhook URL PayPal should call (--via-paypal)")
    args = ap.parse_args()

    if args.via_paypal:
        if not args.target_url:
            ap.error("--via-paypal needs --target-url (your ngrok https URL ending in /webhooks/paypal)")
        client = PayPalClient(get_settings())
        resp = client.request("POST", "/v1/notifications/simulate-event", json_body={
            "url": args.target_url, "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource_version": "2.0"})
        print("PayPal queued the simulated event:", json.dumps(resp))
        return 0

    event = local_event(args.amount)
    resp = httpx.post(args.url, content=json.dumps(event), headers={
        "Content-Type": "application/json", "PAYPAL-TRANSMISSION-ID": str(uuid.uuid4()),
        "PAYPAL-TRANSMISSION-TIME": event["create_time"], "PAYPAL-AUTH-ALGO": "SHA256withRSA"}, timeout=15)
    print(resp.status_code, resp.text)
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
