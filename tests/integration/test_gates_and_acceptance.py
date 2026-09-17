"""C2 acceptance (20): fake-provider runs reach `awaiting_acceptance` with correct gate
results for the pass and fail fixtures, a correction loop works end to end, and an
`artifacts` deliverable reaches `accepted`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from crucible.domain.gates import DEFERRED_MARKER, DEFERRED_TO_C3, GateName
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    correction_document,
    event_kinds,
    review_and_settle,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def gates(client: TestClient, attempt_id: str) -> dict[str, str]:
    body = client.get(f"/v1/attempts/{attempt_id}/gates").json()
    return {row["gate"]: row["result"] for row in body["items"]}


def latest_attempt(client: TestClient, task_id: str) -> str:
    return str(client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"])


async def test_pass_path_reaches_awaiting_acceptance_then_accepted(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["head_sha"] and len(view["head_sha"]) == 40
    attempt_id = view["latest_attempt"]["id"]
    results = gates(client, attempt_id)
    for gate in DEFERRED_TO_C3:
        assert results[gate] == "pending"
    assert results[GateName.INTERNAL_REVIEW_RECORDED] == "pending"
    assert results[GateName.REPORT_PRESENT] == "pass"
    assert results[GateName.EXIT_CLEAN] == "pass"
    assert results[GateName.COMMITS_PRESENT] == "pass"
    assert results[GateName.SCOPE_CONTAINED] == "pass"
    assert results[GateName.NO_SECRETS] == "pass"
    assert results[GateName.RUN_EVIDENCE_PRESENT] == "pass"
    assert results[GateName.CRITERIA_MAPPED] == "pass"

    assert await review_and_settle(supervisor, client, task_id) == "awaiting_acceptance"
    assert gates(client, attempt_id)[GateName.INTERNAL_REVIEW_RECORDED] == "pass"

    accepted = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={
            "verdict": "accepted",
            "reasoning": "The diff does what the contract asked and the evidence shows it.",
            "head_sha": view["head_sha"],
        },
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["state"] == "accepted" and body["publish_pending"] is False
    assert body["acceptance_results"][0]["verdict"] == "accepted"
    kinds = event_kinds(client, task_id)
    for kind in (
        "gates_evaluated",
        "task_awaiting_internal_review",
        "review_report_recorded",
        "task_gates_passed",
        "task_awaiting_acceptance",
        "acceptance_recorded",
        "task_accepted",
    ):
        assert kind in kinds, kind


async def test_deferred_gates_carry_a_marker_and_never_pass(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    rows = client.get(f"/v1/attempts/{latest_attempt(client, task_id)}/gates").json()["items"]
    deferred = [r for r in rows if r["gate"] in DEFERRED_TO_C3]
    assert len(deferred) == len(DEFERRED_TO_C3)
    for row in deferred:
        assert row["result"] == "pending" and DEFERRED_MARKER in row["detail"]


async def test_pull_request_deliverable_stops_at_publish_pending(
    client: TestClient, supervisor: Supervisor
) -> None:
    """09 sends a PR deliverable through `publishing`, which is C4; C2 records the
    acceptance and raises the flag instead."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "Good; publish it."},
    ).json()
    assert body["state"] == "awaiting_acceptance" and body["publish_pending"] is True
    assert "task_publish_pending" in event_kinds(client, task_id)
    wakes = client.get("/v1/wakes").json()["items"]
    assert any(w["reason"] == "publish_pending" for w in wakes)


async def test_fail_path_scope_contained(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-out-of-scope")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempt_id = latest_attempt(client, task_id)
    results = gates(client, attempt_id)
    assert results[GateName.SCOPE_CONTAINED] == "fail"
    assert results[GateName.EXIT_CLEAN] == "pass"
    assert results[GateName.REPORT_PRESENT] == "pass"
    rows = client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    detail = next(r["detail"] for r in rows if r["gate"] == GateName.SCOPE_CONTAINED)
    assert "infrastructure/outside-the-contract.txt" in detail
    summary = client.get(f"/v1/tasks/{task_id}").json()["gate_summary"]
    assert summary["failing"] == [GateName.SCOPE_CONTAINED.value]
    wakes = client.get("/v1/wakes").json()["items"]
    assert any(w["reason"] == "pre_pr_gates_failed" for w in wakes)


@pytest.mark.parametrize(
    ("image", "gate"),
    [
        ("crucible-worker:fake-injected", GateName.NO_INJECTED_FILES),
        ("crucible-worker:fake-secret-leak", GateName.NO_SECRETS),
        ("crucible-worker:fake-no-commits", GateName.COMMITS_PRESENT),
        ("crucible-worker:fake-crash", GateName.EXIT_CLEAN),
        ("crucible-worker:fake-succeed-noreport", GateName.REPORT_PRESENT),
    ],
)
async def test_each_fail_fixture_fails_its_gate(
    client: TestClient, supervisor: Supervisor, image: str, gate: str
) -> None:
    task_id = submit_and_start(client, image, external_id=f"EX-{gate}")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    assert gates(client, latest_attempt(client, task_id))[gate] == "fail"


async def test_a_secret_in_the_diff_is_never_stored(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = latest_attempt(client, task_id)
    rows = client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    detail = next(r["detail"] for r in rows if r["gate"] == GateName.NO_SECRETS)
    assert "github_token" in detail and "ghp_" not in detail
    for artifact in client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]:
        content = client.get(f"/v1/artifacts/{artifact['id']}/content").text
        assert "ghp_" not in content


async def test_correction_loop_from_pre_pr_gates_failed(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-out-of-scope", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    failed_head = client.get(f"/v1/tasks/{task_id}").json()["head_sha"]

    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "scheduled" and r.json()["contract_version"] == 2

    # The default policy sets internal_review.required_for_corrections: false and this
    # correction does not request one, so the corrected head skips the review gate (09).
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_acceptance"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["head_sha"] != failed_head
    roles = [e["role"] for e in view["executions"]]
    assert roles == ["implement", "correct"]
    assert gates(client, latest_attempt(client, task_id))[GateName.INTERNAL_REVIEW_RECORDED] == (
        "skipped"
    )

    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "The correction stayed inside scope."},
    ).json()
    assert body["state"] == "accepted"
    assert "task_correction_attached" in event_kinds(client, task_id)


async def test_needs_more_work_then_a_correction(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "needs_more_work", "reasoning": "AC2 is not really exercised."},
    ).json()
    assert body["state"] == "awaiting_acceptance"
    assert any(w["reason"] == "needs_more_work" for w in client.get("/v1/wakes").json()["items"])

    document = correction_document(
        client, task_id, image="crucible-worker:fake-succeed", reason="needs_more_work"
    )
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 200 and r.json()["state"] == "scheduled"
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_acceptance"


async def test_a_correction_that_widens_scope_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-out-of-scope")
    await run_to_settled(supervisor, client, task_id)
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    document["scope"] = {**document["scope"], "allowed_paths": ["**"]}
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any(e["path"] == "scope.allowed_paths" for e in r.json()["errors"])


async def test_a_correction_is_refused_from_awaiting_acceptance_without_needs_more_work(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any("needs_more_work" in e["message"] for e in r.json()["errors"])


async def test_reject_from_awaiting_acceptance(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "rejected", "reasoning": "The approach is wrong."},
    ).json()
    assert body["state"] == "rejected"


async def test_acceptance_is_refused_before_the_gates_pass(
    client: TestClient, supervisor: Supervisor
) -> None:
    """11: Crucible never infers acceptance, and it is only recorded where 09 allows."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept", json={"verdict": "accepted", "reasoning": "too early"}
    )
    assert r.status_code == 409
    assert r.json()["type"] == "urn:crucible:problem:transition-not-allowed"


async def test_acceptance_for_another_head_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "x", "head_sha": "d" * 40},
    )
    assert r.status_code == 409


async def test_an_observer_may_not_accept(
    client: TestClient, supervisor: Supervisor, tokens: dict[str, str]
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "x"},
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert r.status_code == 403


async def test_blocked_opens_an_escalation_that_a_decision_closes(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert len(view["open_escalations"]) == 1
    escalation = view["open_escalations"][0]
    assert escalation["state"] == "open" and "needs a decision" in escalation["question"]
    assert any(w["reason"] == "blocked" for w in client.get("/v1/wakes").json()["items"])

    r = client.post(
        f"/v1/tasks/{task_id}/decisions",
        json={
            "kind": "scope_clarified",
            "verbatim": "Yes, treat a duplicate as a 409 and carry on.",
            "resolves": escalation["id"],
            "escalation_id": escalation["id"],
            "reschedule": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "scheduled"
    after = client.get(f"/v1/tasks/{task_id}").json()
    assert after["open_escalations"] == []
    assert after["decisions"][0]["verbatim"].startswith("Yes, treat a duplicate")
    kinds = event_kinds(client, task_id)
    assert "escalation_opened" in kinds and "escalation_closed" in kinds


async def test_attempt_metrics_record_the_run(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    await review_and_settle(supervisor, client, task_id)
    client.post(f"/v1/tasks/{task_id}/accept", json={"verdict": "accepted", "reasoning": "good"})
    await supervisor.tick()
    rows = client.get("/v1/routing/history", params={"model": "gpt-5-codex-mini"}).json()["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["harness"] == "codex" and row["pool"] == "openai-sub"
    assert row["exit_class"] == "completed" and row["wall_ms"] is not None
    assert row["gates_passed"] >= 9 and row["gates_failed"] == 0
    assert row["acceptance_verdict"] == "accepted"
    assert row["tokens_out"] is None and row["cost_source"] == "none"
