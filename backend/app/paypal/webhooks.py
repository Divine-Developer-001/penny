"""Money-in layer: PayPal webhook signature verification (self-verify, API postback fallback).

The body is verified as raw bytes BEFORE any JSON parsing: re-serialising changes the CRC32 that
PayPal signs.
"""
from __future__ import annotations

import base64
import json
import logging
import zlib
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import Settings
from app.paypal.client import PayPalClient

logger = logging.getLogger(__name__)
SIMULATOR_WEBHOOK_ID = "WEBHOOK_ID"
_CERT_CACHE: dict[str, bytes] = {}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    source: str          # "self" | "api" | "simulator" | "none"
    reason: str = ""


class VerificationUnavailable(Exception):
    """Self-verification could not run (missing headers, cert fetch failed, odd algorithm)."""


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def crc32_of(raw_body: bytes) -> int:
    return zlib.crc32(raw_body) & 0xFFFFFFFF


def signing_message(transmission_id: str, transmission_time: str, webhook_id: str, raw_body: bytes) -> bytes:
    return f"{transmission_id}|{transmission_time}|{webhook_id}|{crc32_of(raw_body)}".encode()


def _validate_cert_url(url: str) -> None:
    """Only fetch certificates from PayPal hosts (prevents SSRF via a forged header)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (host == "paypal.com" or host.endswith(".paypal.com")):
        raise VerificationUnavailable(f"untrusted cert_url: {url}")


def _fetch_cert(url: str, http: httpx.Client | None) -> bytes:
    if url in _CERT_CACHE:
        return _CERT_CACHE[url]
    _validate_cert_url(url)
    try:
        resp = (http or httpx).get(url, timeout=10.0)
        resp.raise_for_status()
    except Exception as exc:                                  # noqa: BLE001
        raise VerificationUnavailable(f"cert fetch failed: {exc}") from exc
    _CERT_CACHE[url] = resp.content
    return resp.content


def self_verify(headers: Mapping[str, str], raw_body: bytes, webhook_id: str, http: httpx.Client | None = None) -> bool:
    """RSA-SHA256 check of transmissionId|time|webhookId|crc32(body) against PayPal's public cert."""
    h = _lower(headers)
    try:
        tid, ttime = h["paypal-transmission-id"], h["paypal-transmission-time"]
        sig, cert_url = h["paypal-transmission-sig"], h["paypal-cert-url"]
    except KeyError as exc:
        raise VerificationUnavailable(f"missing header {exc}") from exc
    if h.get("paypal-auth-algo", "SHA256withRSA").upper() != "SHA256WITHRSA":
        raise VerificationUnavailable(f"unsupported algorithm {h.get('paypal-auth-algo')}")
    try:
        cert = x509.load_pem_x509_certificate(_fetch_cert(cert_url, http))
        signature = base64.b64decode(sig)
    except VerificationUnavailable:
        raise
    except Exception as exc:                                  # noqa: BLE001
        raise VerificationUnavailable(f"bad certificate or signature encoding: {exc}") from exc
    try:
        cert.public_key().verify(signature, signing_message(tid, ttime, webhook_id, raw_body),
                                 padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False
    except Exception as exc:                                  # noqa: BLE001
        raise VerificationUnavailable(f"verify error: {exc}") from exc


def api_verify(client: PayPalClient, headers: Mapping[str, str], raw_body: bytes, webhook_id: str) -> bool:
    """Postback to /v1/notifications/verify-webhook-signature with the RAW body embedded verbatim."""
    h = _lower(headers)
    head = json.dumps({
        "auth_algo": h.get("paypal-auth-algo", ""), "cert_url": h.get("paypal-cert-url", ""),
        "transmission_id": h.get("paypal-transmission-id", ""), "transmission_sig": h.get("paypal-transmission-sig", ""),
        "transmission_time": h.get("paypal-transmission-time", ""), "webhook_id": webhook_id,
    })
    payload = head[:-1] + ', "webhook_event": ' + raw_body.decode("utf-8") + "}"
    resp = client.request("POST", "/v1/notifications/verify-webhook-signature", content=payload)
    return resp.get("verification_status") == "SUCCESS"


def verify_webhook(headers: Mapping[str, str], raw_body: bytes, settings: Settings,
                   client: PayPalClient | None = None, http: httpx.Client | None = None) -> VerifyResult:
    webhook_id = settings.paypal_webhook_id

    if webhook_id == SIMULATOR_WEBHOOK_ID:
        # PayPal's simulator mock events carry the literal id WEBHOOK_ID: they can be self-verified
        # but not via the postback. Only ever accepted in the sandbox.
        if settings.paypal_env.lower() != "sandbox":
            return VerifyResult(False, "none", "WEBHOOK_ID is only valid in the sandbox")
        try:
            if self_verify(headers, raw_body, SIMULATOR_WEBHOOK_ID, http):
                return VerifyResult(True, "simulator", "self-verified simulator event")
            logger.warning("simulator event failed self-verification; accepting in sandbox mode")
        except VerificationUnavailable as exc:
            logger.info("simulator event not self-verifiable (%s); accepting in sandbox mode", exc)
        return VerifyResult(True, "simulator", "accepted unverified simulator event (sandbox)")

    try:
        if self_verify(headers, raw_body, webhook_id, http):
            return VerifyResult(True, "self", "signature verified locally")
        return VerifyResult(False, "self", "invalid signature")
    except VerificationUnavailable as exc:
        logger.info("self-verification unavailable (%s); falling back to the verify API", exc)
    if client is None:
        return VerifyResult(False, "none", "self-verification unavailable and no PayPal client for fallback")
    try:
        return VerifyResult(True, "api", "verified via PayPal API") if api_verify(client, headers, raw_body, webhook_id) \
            else VerifyResult(False, "api", "PayPal API rejected the signature")
    except Exception as exc:                                  # noqa: BLE001
        return VerifyResult(False, "api", f"verification failed: {exc}")
