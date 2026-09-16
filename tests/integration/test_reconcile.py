"""Reconciliation idempotence, supervisor restart mid-attempt, orphan removal (10, 18)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.ports.execution import LaunchSpec
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import event_kinds, make_supervisor, run_until, submit_and_start

pytestmark = pytest.mark.integration

STATE_TABLES = ("tasks", "executions", "attempts", "events", "completion_claims", "task_contracts")


def snapshot(engine: Engine) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    with engine.connect() as conn:
        for table in STATE_TABLES:
            rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings().all()
            out[table] = [dict(r) for r in rows]
    return out


async def test_reconcile_twice_changes_nothing_after_completion(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    before = snapshot(engine)
    await supervisor.reconcile()
    middle = snapshot(engine)
    await supervisor.reconcile()
    after = snapshot(engine)
    assert before == middle == after


async def test_reconcile_twice_changes_nothing_mid_attempt(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    first = snapshot(engine)
    await supervisor.reconcile()
    second = snapshot(engine)
    await supervisor.reconcile()
    third = snapshot(engine)
    # Lease renewal and the liveness row change; task, execution, attempt, event state do not.
    assert first == second == third
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"


async def test_supervisor_restart_mid_attempt(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a", lease_ttl_seconds=30)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-4")
    await a.tick()
    await a.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "running"
    (attempt,) = view["executions"][0]["attempts"]
    assert attempt["state"] == "running"
    del a  # crash: no release, the lease simply expires

    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b", lease_ttl_seconds=30)
    assert (await b.tick()).held
    assert await run_until(b, client, task_id, {"reported"}) == "reported"
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempts = [x for e in view["executions"] for x in e["attempts"]]
    assert len(attempts) == 1 and attempts[0]["id"] == attempt["id"]
    assert attempts[0]["state"] == "succeeded"
    kinds = event_kinds(client, task_id)
    assert kinds.count("attempt_created") == 1 and kinds.count("attempt_running") == 1
    global_events = client.get("/v1/events", params={"kind": "supervisor_lease_acquired"}).json()
    holders = [e["payload"]["holder"] for e in global_events["items"]]
    assert holders == ["sup-a", "sup-b"]
    assert global_events["items"][-1]["payload"]["takeover"] is True


async def test_restart_between_launch_and_running_adopts_the_worker(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    """Crucible died after the provider launched the worker but before recording it."""
    a = make_supervisor(ctx, provider, holder="sup-a")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-3")
    await a.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    (attempt,) = view["executions"][0]["attempts"]
    # Rewind the record to `launching` with no handle, as if the last write never happened.
    with ctx.uow_factory() as uow:
        uow.set_fenced_token(a.fenced_token or 0)
        row = uow.attempts.get(attempt["id"], for_update=True)
        assert row is not None
        from crucible.domain.lifecycle import AttemptState  # noqa: PLC0415

        row.state = AttemptState.LAUNCHING
        row.handle = None
        row.started_at = None
        uow.attempts.save(row)
        uow.commit()
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    kinds = event_kinds(client, task_id)
    assert "attempt_adopted" in kinds
    assert await run_until(b, client, task_id, {"reported"}) == "reported"


async def test_orphan_handle_is_removed(
    ctx: AppContext, provider: FakeProvider, client: TestClient, supervisor: Supervisor
) -> None:
    spec = LaunchSpec(
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        task_id="none",
        external_id="ORPHAN",
        harness="codex",
        model="m",
        image="crucible-worker:fake-hang",
        timeout_seconds=60,
        contract=contract_document(),
    )
    await provider.launch(await provider.prepare(spec), spec)
    result = await supervisor.tick()
    assert result.orphans == 1
    assert await provider.reconcile() == []
    orphan_events = client.get("/v1/events", params={"kind": "orphan_removed"}).json()["items"]
    assert len(orphan_events) == 1 and orphan_events[0]["attempt_id"] is None


async def test_worker_removed_out_of_band_is_lost(
    client: TestClient, provider: FakeProvider, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-hang",
        lifecycle={"max_attempts": 1, "retry_on": ["lost"], "cleanup": "policy"},
    )
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    provider.remove_out_of_band(str(attempt["id"]))
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    assert "attempt_lost" in event_kinds(client, task_id)
