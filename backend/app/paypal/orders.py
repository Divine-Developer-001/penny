"""Money-in layer: PayPal Orders API v2 (create + capture) for the store checkout."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.paypal.client import PayPalClient, PayPalError


@dataclass(frozen=True)
class CaptureInfo:
    order_status: str
    capture_id: str
    capture_status: str
    amount: Decimal

    @property
    def completed(self) -> bool:
        return self.order_status == "COMPLETED" and self.capture_status == "COMPLETED" and bool(self.capture_id)


def create_order(client: PayPalClient, amount: Decimal, *, reference_id: str, description: str) -> str:
    """Create a CAPTURE-intent order in USD and return its id."""
    created = client.request("POST", "/v2/checkout/orders", json_body={
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": reference_id, "custom_id": reference_id, "description": description[:120],
                            "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}]})
    if "id" not in created:
        raise PayPalError("PayPal did not return an order id")
    return str(created["id"])


def parse_capture(data: dict) -> CaptureInfo:
    unit = (data.get("purchase_units") or [{}])[0]
    cap = ((unit.get("payments") or {}).get("captures") or [{}])[0]
    try:
        amount = Decimal(str((cap.get("amount") or {}).get("value", "0")))
    except InvalidOperation:
        amount = Decimal("0")
    return CaptureInfo(str(data.get("status", "")), str(cap.get("id", "")), str(cap.get("status", "")), amount)


def capture_order(client: PayPalClient, order_id: str) -> CaptureInfo:
    """Capture an approved order. If it was already captured (e.g. we crashed afterwards) read it back instead."""
    try:
        data = client.request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={})
    except PayPalError as exc:
        if "ORDER_ALREADY_CAPTURED" not in str(exc):
            raise
        data = client.request("GET", f"/v2/checkout/orders/{order_id}")
    return parse_capture(data)
