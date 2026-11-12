"""Store layer: Channel3 product search (real offers across retailers), server-side only.

Verified against the Channel3 docs: POST https://api.trychannel3.com/v1/search with an `x-api-key`
header and a JSON body {"query": ...}; the reply has `products[]`, each with `title` and `offers[]`
(`url`, `domain`, `price`). The key never reaches the browser.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.schemas import usd

logger = logging.getLogger(__name__)

BASE_URL = "https://api.trychannel3.com"
SEARCH_ENDPOINT = f"{BASE_URL}/v1/search"


class Channel3Error(RuntimeError):
    pass


def _price(value: Any) -> Decimal | None:
    """Normalize supported USD price formats."""

    if isinstance(value, dict):
        currency = str(
            value.get("currency")
            or value.get("currency_code")
            or "USD"
        ).upper()

        if currency != "USD":
            return None

        value = value.get("price", value.get("amount"))

    if value is None or isinstance(value, bool):
        return None

    try:
        price = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None

    if not price.is_finite() or price <= 0:
        return None

    return usd(price)


def _valid_url(value: Any) -> str | None:
    """Accept only valid HTTP(S) retailer URLs."""

    if not isinstance(value, str):
        return None

    value = value.strip()

    try:
        parsed = urlparse(value)

        if parsed.scheme not in ("http", "https"):
            return None

        if not parsed.netloc or not parsed.hostname:
            return None

        return value

    except ValueError:
        return None


class Channel3Client:

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
    ) -> None:
        self._s = settings
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0,
                read=20.0,
                write=10.0,
                pool=5.0,
            )
        )

    @property
    def configured(self) -> bool:
        return bool(
            str(self._s.channel3_api_key or "").strip()
        )

    def top_offers(
        self,
        query: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:

        if not self.configured:
            raise Channel3Error(
                "CHANNEL3_API_KEY is not configured"
            )

        query = query.strip()

        if not query:
            return []

        if limit <= 0:
            return []

        headers = {
            "x-api-key": self._s.channel3_api_key.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # 1. Make API request
        try:
            response = self._http.post(
                SEARCH_ENDPOINT,
                headers=headers,
                json={"query": query},
            )

            response.raise_for_status()

        except httpx.TimeoutException as exc:
            logger.error("Channel3 request timed out")
            raise Channel3Error(
                "Channel3 request timed out"
            ) from exc

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code

            logger.error(
                "Channel3 returned HTTP %s: %s",
                status,
                exc.response.text[:500],
            )

            raise Channel3Error(
                f"Channel3 HTTP error: {status}"
            ) from exc

        except httpx.HTTPError as exc:
            logger.exception("Channel3 connection failed")
            raise Channel3Error(
                f"Channel3 connection failed: {exc}"
            ) from exc

        # 2. Parse JSON response
        try:
            data = response.json()
        except ValueError as exc:
            logger.error("Channel3 returned invalid JSON")
            raise Channel3Error(
                "Channel3 returned invalid JSON"
            ) from exc

        if not isinstance(data, dict):
            raise Channel3Error(
                "Unexpected Channel3 response format"
            )

        products = data.get("products")

        if not isinstance(products, list):
            logger.warning(
                "Channel3 response missing products array"
            )
            return []

        # 3. Extract offers from all products
        candidates: list[dict[str, Any]] = []

        for product in products:

            if not isinstance(product, dict):
                continue

            title = str(
                product.get("title") or query
            ).strip()[:140]

            offers = product.get("offers") or []

            if not isinstance(offers, list):
                continue

            for offer in offers:

                if not isinstance(offer, dict):
                    continue

                price = _price(offer.get("price"))
                link = _valid_url(offer.get("url"))

                if price is None or link is None:
                    continue

                candidates.append({
                    "title": title,
                    "merchant": str(
                        offer.get("domain")
                        or offer.get("merchant_name")
                        or "Retailer"
                    ),
                    "price": price,
                    "link": link,
                })

        # 4. Rank offers globally by price
        candidates.sort(
            key=lambda item: item["price"]
        )

        results = candidates[:limit]

        logger.info(
            "Channel3 search completed: query=%r products=%d valid_offers=%d returned=%d",
            query,
            len(products),
            len(candidates),
            len(results),
        )

        return results
