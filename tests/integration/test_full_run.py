"""Acceptance for C1: a submitted task runs through the fake provider to reported and the
event log tells the whole story."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from tests.integration.conftest import event_kinds, run_until, submit_and_start

pytestmark = pytest.mark.integration


async def test_submit_start_run_to_reported(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-2")
    state = await run_until(supervisor, client, task_id, {"reported"})
    assert state == "reported"

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["schema_version"] == "1.0"
    assert len(view["executions"]) == 1
    execution = view["executions"][0]
    assert execution["state"] == "succeeded"
    assert len(execution["attempts"]) == 1
    attempt = execution["attempts"][0]
    assert attempt["state"] == "succeeded"
    assert attempt["exit_code"] == 0 and attempt["exit_class"] == "completed"
    assert attempt["started_at"].endswith("+00:00")
    assert view["latest_attempt"]["id"] == attempt["id"]
    assert view["contract_versions"][0]["version"] == 1

    assert event_kinds(client, task_id) == [
        "task_submitted",
        "task_scheduled",
        "execution_created",
        "attempt_created",
        "attempt_preparing",
        "execution_active",
        "task_running",
        "attempt_launching",
        "attempt_running",
        "attempt_exited",
        "report_parsed",
        "attempt_collected",
        "attempt_succeeded",
        "execution_succeeded",
        "task_reported",
    ]
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    principals = {e["kind"]: e["principal"] for e in events}
    assert principals["task_submitted"] == "orchestrator-principal"
    assert principals["task_scheduled"] == "orchestrator-principal"
    assert all(
        e["principal"] == "crucible"
        for e in events
        if e["kind"].startswith(("attempt_", "execution_"))
    )
    reported = next(e for e in events if e["kind"] == "task_reported")
    assert reported["payload"]["exit_class"] == "completed"
    assert reported["attempt_id"] == attempt["id"]
    assert all(e["ts"].endswith("+00:00") for e in events)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)

    parsed = client.get(f"/v1/attempts/{attempt['id']}").json()
    assert parsed["report"]["parsed_ok"] is True
    assert parsed["report"]["document"]["task_external_id"] == "EX-0001"
    assert parsed["lease"] is None

    ex = client.get(f"/v1/executions/{execution['id']}").json()
    assert ex["state"] == "succeeded" and ex["policy_snapshot"]["name"] == "default-software"

    ready = client.get("/v1/ready")
    assert ready.status_code == 200, ready.text
    sup = client.get("/v1/supervisor").json()
    assert sup["lease"]["holder"] == "sup-a"
    assert sup["counts"]["tasks_running"] == 0
    assert sup["tick_ms"] is not None
    assert sup["providers"][0]["name"] == "fake"
    await supervisor.stop()
    assert client.get("/v1/ready").status_code == 503


async def test_task_view_before_first_tick(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "scheduled" and view["executions"] == []
    assert view["latest_attempt"] is None
