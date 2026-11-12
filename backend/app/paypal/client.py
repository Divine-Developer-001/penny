"""Money-in/out layer: minimal PayPal REST client (OAuth client-credentials, cached token)."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class PayPalError(RuntimeError):
    pass


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._s = settings
        self._http = http or httpx.Client(timeout=20.0)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def base(self) -> str:
        return self._s.paypal_api_base.rstrip("/")

    def get_token(self) -> str:
        """OAuth token, cached until 60s before it expires."""
        if self._token and self._clock() < self._expires_at:
            return self._token
        if not self._s.paypal_configured:
            raise PayPalError("PayPal credentials are not configured (see .env.example)")
        resp = self._http.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                               auth=(self._s.paypal_client_id, self._s.paypal_client_secret),
                               headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise PayPalError(f"OAuth failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = self._clock() + int(data.get("expires_in", 300)) - 60
        return self._token

    def request(self, method: str, path: str, *, json_body: Any = None, content: str | None = None) -> dict[str, Any]:
        """Authenticated call. Pass `content` to send an exact raw JSON string (no re-serialising)."""
        headers = {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        resp = self._http.request(method, f"{self.base}{path}", json=json_body, content=content, headers=headers)
        if resp.status_code >= 400:
            raise PayPalError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}
