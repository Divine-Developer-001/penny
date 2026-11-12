"""Store layer: Channel3 product search (real offers across retailers), server-side only.

Verified against the Channel3 docs: POST https://api.trychannel3.com/v1/search with an `x-api-key`
header and a JSON body {"query": ...}; the reply has `products[]`, each with `title` and `offers[]`
(`url`, `domain`, `price`). The key never reaches the browser.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.schemas import usd

logger = logging.getLogger(__name__)
BASE_URL = "https://api.trychannel3.com"


class Channel3Error(RuntimeError):
    pass


def _price(value: Any) -> Decimal | None:
    """Offer prices arrive as a number, a string, or an object like {"price": 49.0, "currency": "USD"}."""
    if isinstance(value, dict):
        currency = str(value.get("currency") or value.get("currency_code") or "USD").upper()
        if currency != "USD":
            return None
        value = value.get("price", value.get("amount"))
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return usd(d) if d > 0 else None


class Channel3Client:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._s = settings
        self._http = http or httpx.Client(timeout=12.0)

    @property
    def configured(self) -> bool:
        return bool(self._s.channel3_api_key.strip())

    def top_offers(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Best-ranked offers for a natural-language query: [{title, merchant, price, link}]."""
        if not self.configured:
            raise Channel3Error("CHANNEL3_API_KEY is not set")
        try:
            resp = self._http.post(f"{BASE_URL}/v1/search", headers={"x-api-key": self._s.channel3_api_key.strip(),
                                                                       "Content-Type": "application/json"},
                                   json={"query": query})
        except httpx.HTTPError as exc:
            raise Channel3Error(f"Channel3 unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise Channel3Error(f"Channel3 HTTP {resp.status_code}: {resp.text[:200]}")
        out: list[dict[str, Any]] = []
        for product in resp.json().get("products") or []:
            offers = [(o, _price(o.get("price"))) for o in (product.get("offers") or [])]
            for offer, price in sorted((x for x in offers if x[1] is not None), key=lambda x: x[1]):
                out.append({"title": str(product.get("title") or query)[:140],
                            "merchant": str(offer.get("domain") or offer.get("merchant_name") or "retailer"),
                            "price": price, "link": offer.get("url")})
                if len(out) >= limit:
                    return out
        return out
