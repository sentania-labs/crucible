"""Wake delivery mechanics (17) and correction-version narrowing (05)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from crucible.adapters.notification.webhook import SIGNATURE_HEADER, sign
from crucible.application.wakes import (
    DEFAULT_WAKE_RETRY_HOURS,
    RETRY_BACKOFF_SECONDS,
    next_backoff,
    retry_hours_from_policy,
    wake_document,
)
from crucible.contracts.task_contract import TaskContractV1, correction_narrows
from crucible.contracts.wake import WakeReason, WakeV1
from crucible.domain.entities import Wake
from tests.fixtures import FakeClock, contract_document


def _wake(reason: WakeReason = WakeReason.GATES_PASSED) -> Wake:
    return Wake(
        id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        principal_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        task_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        reason=reason.value,
        payload={
            "summary": "every required pre-PR gate passed",
            "task": {
                "id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
                "external_id": "EX-0001",
                "state": "awaiting_acceptance",
            },
            "attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAY",
            "links": {"task": "/v1/tasks/01ARZ3NDEKTSV4RRFFQ69G5FAX"},
        },
        created_at=FakeClock().now(),
    )


def test_wake_document_is_a_valid_wake_v1() -> None:
    document = wake_document(_wake(), principal_name="foundry")
    model = WakeV1.model_validate(document)
    assert model.principal == "foundry"
    assert model.reason is WakeReason.GATES_PASSED
    assert model.task is not None and model.task.external_id == "EX-0001"
    assert model.links["task"].startswith("/v1/tasks/")
    assert document["created_at"].endswith("+00:00")


def test_backoff_schedule_is_bounded_and_monotonic() -> None:
    values = [next_backoff(n) for n in range(1, len(RETRY_BACKOFF_SECONDS) + 3)]
    assert values[: len(RETRY_BACKOFF_SECONDS)] == list(RETRY_BACKOFF_SECONDS)
    assert values[-1] == RETRY_BACKOFF_SECONDS[-1]
    assert values == sorted(values)


def test_retry_hours_come_from_policy_limits() -> None:
    assert retry_hours_from_policy(None) == DEFAULT_WAKE_RETRY_HOURS
    assert retry_hours_from_policy({"limits": {"wake_retry_hours": 6}}) == 6


def test_signature_is_an_hmac_over_the_raw_body() -> None:
    secret = "s3cr3t-for-this-test-only"
    body = json.dumps({"id": "x"}).encode()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sign(secret, body) == f"sha256={expected}"
    assert SIGNATURE_HEADER == "X-Crucible-Signature-256"
    # A different body produces a different signature.
    assert sign(secret, body + b" ") != sign(secret, body)


def _contract(**patch: Any) -> TaskContractV1:
    document = contract_document()
    for path, value in patch.items():
        section, _, field = path.partition(".")
        if field:
            document[section][field] = value
        else:
            document[section] = value
    return TaskContractV1.model_validate(document)


def _correction(**patch: Any) -> TaskContractV1:
    document = contract_document()
    document["correction"] = {
        "of_version": 1,
        "reason": "pre_pr_gates",
        "addresses": [{"kind": "internal_review", "id": "1", "disposition_id": None}],
        "instructions": "Keep the change inside src/ledger.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    for path, value in patch.items():
        section, _, field = path.partition(".")
        if field:
            document[section][field] = value
        else:
            document[section] = value
    return TaskContractV1.model_validate(document)


def test_an_identical_correction_narrows() -> None:
    assert correction_narrows(_contract(), _correction()) == []


def test_a_correction_may_narrow_scope() -> None:
    narrowed = _correction(**{"scope.allowed_paths": ["src/ledger/**"]})
    assert correction_narrows(_contract(), narrowed) == []


def test_a_correction_may_not_widen_allowed_paths() -> None:
    widened = _correction(
        **{"scope.allowed_paths": ["src/ledger/**", "tests/ledger/**", "docs/**"]}
    )
    problems = correction_narrows(_contract(), widened)
    assert any(p["path"] == "scope.allowed_paths" for p in problems)


def test_a_correction_may_not_drop_a_prohibition() -> None:
    dropped = _correction(**{"scope.prohibited_paths": []})
    problems = correction_narrows(_contract(), dropped)
    assert any(p["path"] == "scope.prohibited_paths" for p in problems)


@pytest.mark.parametrize("flag", ["may_add_dependencies", "may_modify_ci"])
def test_a_correction_may_not_turn_on_a_scope_flag(flag: str) -> None:
    widened = _correction(**{f"scope.{flag}": True})
    problems = correction_narrows(_contract(), widened)
    assert any(p["path"] == f"scope.{flag}" for p in problems)


def test_required_verification_may_not_shrink() -> None:
    document = contract_document()
    shrunk = _correction(**{"required_verification": document["required_verification"][:2]})
    problems = correction_narrows(_contract(), shrunk)
    assert any("may not shrink" in p["message"] for p in problems)


def test_a_correction_keeps_the_task_identity() -> None:
    moved = _correction(**{"external_id": "EX-9999"})
    problems = correction_narrows(_contract(), moved)
    assert any(p["path"] == "external_id" for p in problems)


def test_webhook_deliverer_is_unconfigured_without_a_url() -> None:
    from crucible.adapters.notification.webhook import WebhookWakeDeliverer  # noqa: PLC0415

    assert WebhookWakeDeliverer(None, "s").configured is False
    assert WebhookWakeDeliverer("https://foundry.invalid/wake", None).configured is True


async def test_an_unconfigured_deliverer_reports_poll_only() -> None:
    from crucible.adapters.notification.webhook import WebhookWakeDeliverer  # noqa: PLC0415

    result = await WebhookWakeDeliverer(None, None).deliver(b"{}")
    assert result.ok is False and "poll only" in result.detail


async def test_delivery_signs_the_body_and_reports_the_status() -> None:
    """The signature covers the raw body, and the secret never appears in the request."""
    import http.server  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from crucible.adapters.notification.webhook import (  # noqa: PLC0415
        SIGNATURE_HEADER,
        WebhookWakeDeliverer,
    )

    seen: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            seen["body"] = self.rfile.read(length)
            seen["headers"] = dict(self.headers)
            self.send_response(503 if seen.get("fail") else 204)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/wake"
        secret = "wake-secret-for-this-test-only"
        body = json.dumps({"id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}).encode()
        result = await WebhookWakeDeliverer(url, secret).deliver(body)
        assert result.ok is True and result.detail == "HTTP 204"
        assert seen["body"] == body
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert seen["headers"][SIGNATURE_HEADER] == f"sha256={expected}"
        assert secret not in str(seen["headers"])

        seen["fail"] = True
        failed = await WebhookWakeDeliverer(url, secret).deliver(body)
        assert failed.ok is False and failed.detail == "HTTP 503"
    finally:
        server.shutdown()
        server.server_close()


async def test_delivery_to_a_dead_receiver_is_a_failure_not_an_exception() -> None:
    from crucible.adapters.notification.webhook import WebhookWakeDeliverer  # noqa: PLC0415

    # Port 1 on loopback refuses immediately.
    result = await WebhookWakeDeliverer("http://127.0.0.1:1/wake", None).deliver(b"{}")
    assert result.ok is False and result.detail
