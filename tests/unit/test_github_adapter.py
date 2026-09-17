"""The GitHub adapter's pure parts: the App JWT, normalization, and the webhook (23).

Every secret-shaped value here is built at run time. Nothing in this repository is a
checked-in token, key, or signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from crucible.adapters.github import normalize
from crucible.adapters.github.appauth import AppKeyError, load_private_key, sign_jwt
from crucible.adapters.github.webhook import (
    HANDLED_EVENTS,
    SignatureError,
    body_digest,
    normalize_delivery,
    verify_signature,
)
from crucible.domain.secrets import scan_text
from crucible.ports.github import GitHubError, InstallationToken, classify
from tests.integration.fake_github import installation_token_value

SECRET = "not-a-real-secret-only-this-test"


def _key(tmp_path, name: str = "app.pem") -> str:  # type: ignore[no-untyped-def]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / name
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(path)


def _decode(segment: str) -> dict[str, object]:
    padded = segment + "=" * (-len(segment) % 4)
    return dict(json.loads(base64.urlsafe_b64decode(padded.encode())))


def test_the_app_jwt_is_rs256_backdated_and_verifies(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = _key(tmp_path)
    key = load_private_key(path)
    now = time.time()
    token = sign_jwt(4969317, key, now=now)
    header_b64, payload_b64, signature_b64 = token.split(".")
    assert _decode(header_b64) == {"alg": "RS256", "typ": "JWT"}
    payload = _decode(payload_b64)
    assert payload["iss"] == "4969317"
    assert int(payload["iat"]) == int(now) - 60  # type: ignore[call-overload]
    assert int(payload["exp"]) > int(now)  # type: ignore[call-overload]
    signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    key.public_key().verify(
        signature,
        f"{header_b64}.{payload_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_a_missing_or_unusable_key_is_refused_by_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AppKeyError, match="could not be read"):
        load_private_key(str(tmp_path / "absent.pem"))
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key", encoding="utf-8")
    with pytest.raises(AppKeyError, match="not a usable PEM"):
        load_private_key(str(junk))


def test_a_token_never_prints_itself() -> None:
    """12: an accidental f-string must not put a token in a log."""
    value = installation_token_value()
    token = InstallationToken(value, expires_at=datetime.now(UTC), repository="o/r")
    assert value not in repr(token)
    assert value not in f"{token}"
    assert value not in str(token)
    assert token.reveal() == value
    token.discard()
    assert token.reveal() == ""


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (0, "transport"),
        (401, "forbidden"),
        (403, "forbidden"),
        (404, "not_found"),
        (422, "unprocessable"),
        (429, "rate_limited"),
        (418, "client_error"),
        (502, "server_error"),
        (200, "ok"),
    ],
)
def test_a_failure_is_recorded_as_a_class_not_a_body(status: int, expected: str) -> None:
    assert classify(status) == expected
    assert GitHubError(status, "boom", path="/x").response_class == expected


# ----- webhooks ---------------------------------------------------------


def signed(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_the_signature_is_verified_against_the_raw_body() -> None:
    body = b'{"action":"opened"}'
    verify_signature(SECRET, body, signed(body))
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(SECRET, body + b" ", signed(body))
    with pytest.raises(SignatureError, match="no sha256"):
        verify_signature(SECRET, body, None)
    with pytest.raises(SignatureError, match="no webhook secret"):
        verify_signature("", body, signed(body))


def test_an_unhandled_event_normalizes_to_nothing() -> None:
    assert normalize_delivery(delivery_id="d", event="star", raw_body=b"{}") is None
    assert "pull_request" in HANDLED_EVENTS and "star" not in HANDLED_EVENTS


def test_only_the_fields_crucible_uses_survive_normalization() -> None:
    raw = json.dumps(
        {
            "action": "synchronize",
            "repository": {"full_name": "o/r", "private": True, "id": 42},
            "sender": {"login": "someone", "email": "someone@example.invalid"},
            "pull_request": {
                "number": 7,
                "html_url": "https://github.com/o/r/pull/7",
                "state": "open",
                "title": "a title",
                "head": {"sha": "a" * 40, "ref": "crucible/x"},
                "base": {"ref": "main"},
                "user": {"login": "crucible-spike[bot]", "id": 1},
            },
        }
    ).encode("utf-8")
    delivery = normalize_delivery(delivery_id="d-1", event="pull_request", raw_body=raw)
    assert delivery is not None
    assert delivery.body_sha256 == body_digest(raw)
    assert delivery.normalized["pull_request"]["head_sha"] == "a" * 40
    blob = json.dumps(delivery.normalized)
    assert "someone@example.invalid" not in blob
    assert "private" not in blob


def test_a_review_delivery_asks_for_an_immediate_reaction_poll() -> None:
    raw = json.dumps(
        {
            "action": "submitted",
            "repository": {"full_name": "o/r"},
            "review": {
                "id": 1,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "state": "COMMENTED",
                "body": "findings",
                "commit_id": "a" * 40,
                "submitted_at": "2026-09-16T21:18:13Z",
            },
        }
    ).encode("utf-8")
    delivery = normalize_delivery(delivery_id="d-2", event="pull_request_review", raw_body=raw)
    assert delivery is not None and delivery.triggers_reaction_poll
    assert delivery.normalized["review"]["commit_id"] == "a" * 40


def test_user_controlled_text_is_scanned_and_redacted_before_it_is_kept() -> None:
    value = installation_token_value()
    body, hit = normalize.clean_text(f"pushed with {value}")
    assert hit == "github_installation_token"
    assert value not in body
    assert scan_text(body) is None


def test_a_push_delivery_keeps_the_ref_and_both_ends() -> None:
    raw = json.dumps(
        {
            "repository": {"full_name": "o/r"},
            "ref": "refs/heads/crucible/x",
            "before": "a" * 40,
            "after": "b" * 40,
            "pusher": {"name": "someone"},
        }
    ).encode("utf-8")
    delivery = normalize_delivery(delivery_id="d-3", event="push", raw_body=raw)
    assert delivery is not None
    assert delivery.normalized["after"] == "b" * 40
    assert delivery.normalized["pusher"] == "someone"


def test_a_check_run_delivery_normalizes_to_the_certification_fields() -> None:
    raw = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "o/r"},
            "check_run": {
                "id": 9001,
                "name": "build",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "a" * 40,
                "html_url": "https://github.com/o/r/runs/9001",
                "app": {"slug": "github-actions"},
            },
        }
    ).encode("utf-8")
    delivery = normalize_delivery(delivery_id="d-4", event="check_run", raw_body=raw)
    assert delivery is not None
    assert delivery.normalized["check"] == {
        "name": "build",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "a" * 40,
        "url": "https://github.com/o/r/runs/9001",
        "external_id": "9001",
        "workflow": "github-actions",
        "source": "check_run",
    }
