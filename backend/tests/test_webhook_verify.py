"""Signature verification with a locally generated certificate (no network)."""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.paypal import webhooks
from app.paypal.webhooks import VerificationUnavailable, self_verify, signing_message, verify_webhook
from tests.conftest import make_settings

CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-TEST"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "paypal-test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


def signed(key, body: bytes, webhook_id: str, tid="TID-1", ttime="2026-11-04T12:00:00Z"):
    sig = key.sign(signing_message(tid, ttime, webhook_id, body), padding.PKCS1v15(), hashes.SHA256())
    return {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": CERT_URL, "PAYPAL-TRANSMISSION-ID": tid,
            "PAYPAL-TRANSMISSION-TIME": ttime, "PAYPAL-TRANSMISSION-SIG": base64.b64encode(sig).decode()}


@pytest.fixture(autouse=True)
def cert_cache(keypair):
    webhooks._CERT_CACHE.clear()
    webhooks._CERT_CACHE[CERT_URL] = keypair[1]
    yield
    webhooks._CERT_CACHE.clear()


BODY = json.dumps({"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED"}).encode()


def test_valid_signature_verifies(keypair):
    assert self_verify(signed(keypair[0], BODY, "WH-REAL"), BODY, "WH-REAL") is True


def test_tampered_body_fails(keypair):
    headers = signed(keypair[0], BODY, "WH-REAL")
    assert self_verify(headers, BODY.replace(b"WH-1", b"WH-2"), "WH-REAL") is False


def test_wrong_webhook_id_fails(keypair):
    assert self_verify(signed(keypair[0], BODY, "WH-REAL"), BODY, "WH-OTHER") is False


def test_crc_depends_on_raw_bytes_not_parsed_json(keypair):
    reformatted = json.dumps(json.loads(BODY), indent=2).encode()           # same JSON, different bytes
    assert self_verify(signed(keypair[0], BODY, "WH-REAL"), reformatted, "WH-REAL") is False


def test_untrusted_cert_host_is_never_fetched(keypair):
    webhooks._CERT_CACHE.clear()
    headers = signed(keypair[0], BODY, "WH-REAL") | {"PAYPAL-CERT-URL": "https://evil.example.com/cert.pem"}
    with pytest.raises(VerificationUnavailable):
        self_verify(headers, BODY, "WH-REAL")
    headers["PAYPAL-CERT-URL"] = "http://api.paypal.com/cert.pem"          # not https
    with pytest.raises(VerificationUnavailable):
        self_verify(headers, BODY, "WH-REAL")


def test_verify_webhook_real_id_accepts_good_and_rejects_bad(keypair):
    s = make_settings(paypal_webhook_id="WH-REAL")
    assert verify_webhook(signed(keypair[0], BODY, "WH-REAL"), BODY, s).source == "self"
    bad = verify_webhook(signed(keypair[0], BODY, "WH-REAL"), BODY + b" ", s)
    assert not bad.ok and bad.reason == "invalid signature"


def test_missing_headers_fall_back_to_the_verify_api():
    class FakeClient:
        def request(self, method, path, *, json_body=None, content=None):
            assert path == "/v1/notifications/verify-webhook-signature"
            sent = json.loads(content)
            assert sent["webhook_id"] == "WH-REAL" and sent["webhook_event"]["id"] == "WH-1"
            return {"verification_status": "SUCCESS"}

    s = make_settings(paypal_webhook_id="WH-REAL")
    result = verify_webhook({}, BODY, s, client=FakeClient())
    assert result.ok and result.source == "api"
    assert not verify_webhook({}, BODY, s, client=None).ok


def test_simulator_events_are_accepted_in_sandbox_only():
    s = make_settings()                                                    # WEBHOOK_ID + sandbox
    ok = verify_webhook({}, BODY, s)
    assert ok.ok and ok.source == "simulator"
    s.paypal_env = "live"
    assert not verify_webhook({}, BODY, s).ok


def test_simulator_events_can_be_self_verified_with_the_literal_id(keypair):
    result = verify_webhook(signed(keypair[0], BODY, "WEBHOOK_ID"), BODY, make_settings())
    assert result.ok and result.reason == "self-verified simulator event"
