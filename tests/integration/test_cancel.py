from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from tests.fixtures import FakeClock
from tests.integration.conftest import event_kinds, run_until, submit_and_start

pytestmark = pytest.mark.integration

CANCEL = {"reason": "scope changed", "verbatim": "stop that one please", "decided_by": "scott"}


def _cancel(client: TestClient, task_id: str) -> dict[str, object]:
    r = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_cancel_running_task_drains_then_cancels(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider, clock: FakeClock
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    assert _cancel(client, task_id)["state"] == "cancelling"
    await supervisor.tick()
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None and worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt['id']}").json()["state"] == "terminating"
    clock.advance(61)
    await supervisor.tick()
    assert worker.kills == 1
    assert await run_until(supervisor, client, task_id, {"cancelled"}) == "cancelled"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["state"] == "cancelled"
    assert view["latest_attempt"]["exit_class"] == "killed"
    assert view["closed_at"] is not None
    kinds = event_kinds(client, task_id)
    assert "task_cancel_requested" in kinds and kinds[-1] == "task_cancelled"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    req = next(e for e in events if e["kind"] == "task_cancel_requested")
    assert req["payload"]["verbatim"] == "stop that one please"
    assert req["principal"] == "orchestrator-principal"


async def test_cancel_submitted_task_is_immediate(client: TestClient) -> None:
    from tests.fixtures import contract_document  # noqa: PLC0415

    r = client.post("/v1/tasks", json=contract_document())
    task_id = r.json()["id"]
    assert _cancel(client, task_id)["state"] == "cancelled"
    assert event_kinds(client, task_id) == [
        "task_submitted",
        "task_cancel_requested",
        "task_cancelled",
    ]


async def test_cancel_scheduled_task_settles_pending_attempt(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert _cancel(client, task_id)["state"] == "cancelled"
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "cancelled" and view["executions"] == []


async def test_cancel_reported_task_is_rejected(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    r = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:crucible:problem:transition-not-allowed"
    assert "reported -> cancelled" in body["detail"]
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    rejected = [e for e in events if e["kind"] == "transition_rejected"]
    assert len(rejected) == 1 and rejected[0]["payload"]["to"] == "cancelled"
