"""Demo script (money-out): trigger PayPal's sandbox negative test ERRPYO002 (SENDER_EMAIL_UNCONFIRMED).

Creates a payout whose item note is ERRPYO002, then reads the item status back so you can show how
Penny reacts to a failed payout (cash restored to the pot, explanation logged). To see that end to
end through the app instead, set PAYPAL_NEGATIVE_TEST=true in .env and call POST /withdraw.
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.paypal import payouts  # noqa: E402
from app.paypal.client import PayPalClient, PayPalError  # noqa: E402


def main() -> int:
    settings = get_settings().model_copy(update={"paypal_negative_test": True})
    client = PayPalClient(settings)
    try:
        info = payouts.create_payout(client, settings, Decimal("1.00"))
        print("created:", json.dumps(info))
        item_id = info["payout_item_id"]
        for _ in range(10):
            if item_id:
                item = payouts.get_payout_item(client, item_id)
                print("item status:", item.get("transaction_status"), json.dumps(item.get("errors") or {}))
                if item.get("transaction_status") not in (None, "PENDING"):
                    break
            time.sleep(3)
    except PayPalError as exc:
        print("PayPal error (this is also a valid negative-test outcome):", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
