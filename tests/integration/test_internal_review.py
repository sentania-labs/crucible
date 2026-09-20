"""Internal non-author review (04, 11): upload and the `review` execution role, with
reviewer_must_not_be_author enforced mechanically."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.domain.gates import GateName
from tests.integration.conftest import (
    event_kinds,
    review_and_settle,
    run_to_settled,
    submit_and_start,
    upload_review,
)

pytestmark = pytest.mark.integration

REVIEW_EXECUTION = {
    "harness": "codex",
    "model": "gpt-5.6-luna",
    "provider": "fake",
    "image": "crucible-worker:fake-review",
    "timeout_seconds": 600,
    "rationale": "A non-author review of the collected head.",
}


def gates(client: TestClient, attempt_id: str) -> dict[str, str]:
    body = client.get(f"/v1/attempts/{attempt_id}/gates").json()
    return {row["gate"]: row["result"] for row in body["items"]}


async def test_review_execution_produces_a_report_and_passes_the_gate(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    view = client.get(f"/v1/tasks/{task_id}").json()
    head = view["head_sha"]
    author_attempt = view["latest_attempt"]["id"]

    r = client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "awaiting_internal_review"

    state = await run_to_settled(supervisor, client, task_id, max_ticks=8)
    assert state == "awaiting_acceptance"
    view = client.get(f"/v1/tasks/{task_id}").json()
    roles = {e["role"]: e for e in view["executions"]}
    assert set(roles) == {"implement", "review"}
    assert roles["review"]["state"] == "succeeded"
    reviewer_attempt = roles["review"]["attempts"][0]["id"]
    assert reviewer_attempt != author_attempt

    report = view["review_reports"][0]
    assert report["head_sha"] == head
    assert report["reviewer_kind"] == "crucible_review_execution"
    assert report["reviewer_attempt_id"] == reviewer_attempt
    assert report["verdict"] == "approve"
    assert gates(client, author_attempt)[GateName.INTERNAL_REVIEW_RECORDED] == "pass"
    kinds = event_kinds(client, task_id)
    assert "review_execution_requested" in kinds and "review_report_recorded" in kinds


async def test_a_request_changes_verdict_does_not_move_the_task_by_itself(
    client: TestClient, supervisor: Supervisor
) -> None:
    """11: Foundry reads the verdict and decides; the gate only needs a non-author report."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    assert await review_and_settle(supervisor, client, task_id, verdict="request_changes") == (
        "awaiting_acceptance"
    )
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["review_reports"][0]["verdict"] == "request_changes"
    assert view["review_reports"][0]["findings"] == 1


async def test_a_review_for_another_head_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    r = upload_review(client, task_id, head_sha="e" * 40)
    assert r.status_code == 422
    assert any(e["path"] == "reviewed_head_sha" for e in r.json()["errors"])
    assert "review_report_rejected" in event_kinds(client, task_id)


async def test_an_uploaded_report_may_not_claim_a_review_execution(
    client: TestClient, supervisor: Supervisor
) -> None:
    """reviewer_must_not_be_author is mechanical (11): an upload is the orchestrator's,
    and only Crucible may record a report as a review execution's."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    author = view["latest_attempt"]["id"]
    report = {
        "schema_version": "1.0",
        "task_external_id": view["external_id"],
        "reviewed_head_sha": view["head_sha"],
        "reviewer": {"kind": "crucible_review_execution", "attempt_id": author},
        "verdict": "approve",
        "findings": [],
        "summary": "I reviewed myself.",
    }
    r = client.post(f"/v1/tasks/{task_id}/review", json={"report": report})
    assert r.status_code == 403
    assert r.json()["errors"][0]["path"] == "reviewer.kind"
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_internal_review"
    assert "review_report_rejected" in event_kinds(client, task_id)


async def test_a_review_worker_may_not_name_another_attempt_as_the_reviewer(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """The supervisor knows which attempt ran the review; the document may only agree.

    A worker that names the implementing attempt would be asserting its own
    non-authorship, which is the one thing this constraint may not take on trust."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", external_id="EX-FORGE")
    await run_to_settled(supervisor, client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    author = view["latest_attempt"]["id"]
    provider.set_report(
        "EX-FORGE",
        {
            "schema_version": "1.0",
            "task_external_id": "EX-FORGE",
            "reviewed_head_sha": view["head_sha"],
            "reviewer": {"kind": "crucible_review_execution", "attempt_id": author},
            "verdict": "approve",
            "findings": [],
            "summary": "The author signing off on itself.",
        },
    )
    client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    for _ in range(4):
        await supervisor.tick()
    after = client.get(f"/v1/tasks/{task_id}").json()
    assert after["state"] == "awaiting_internal_review"
    assert after["review_reports"] == []
    review = next(e for e in after["executions"] if e["role"] == "review")
    assert review["state"] == "failed"
    assert "review_report_rejected" in event_kinds(client, task_id)
    assert gates(client, author)[GateName.INTERNAL_REVIEW_RECORDED] == "pending"


async def test_an_unparsable_review_is_refused_with_paths(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    r = client.post(f"/v1/tasks/{task_id}/review", json={"report": {"schema_version": "1.0"}})
    assert r.status_code == 422
    assert {tuple(e["loc"]) for e in r.json()["errors"]} >= {("verdict",), ("summary",)}


async def test_review_is_refused_outside_awaiting_internal_review(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    assert r.status_code == 409


async def test_review_body_must_name_exactly_one_of_report_or_execution(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    assert client.post(f"/v1/tasks/{task_id}/review", json={}).status_code == 422
    both = {"report": {"schema_version": "1.0"}, "execution": REVIEW_EXECUTION}
    assert client.post(f"/v1/tasks/{task_id}/review", json=both).status_code == 422


async def test_a_review_execution_is_created_once(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    await supervisor.tick()
    await supervisor.tick()
    executions = client.get(f"/v1/tasks/{task_id}").json()["executions"]
    assert [e["role"] for e in executions].count("review") == 1
