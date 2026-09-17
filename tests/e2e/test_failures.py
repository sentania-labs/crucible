"""Timeout, loss, orphan, restart, disconnect, and the checkout lease (18)."""

from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerProvider
from crucible.application.supervisor import Supervisor
from tests.e2e import daemon
from tests.e2e.conftest import (
    NET_WORKERS,
    RUN_ID,
    OriginFactory,
    e2e_contract,
    event_kinds,
    register,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.e2e

DONE = {"awaiting_internal_review", "gates_passed", "awaiting_acceptance", "pre_pr_gates_failed"}


async def _attempt_id(client: TestClient, task_id: str) -> str:
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["latest_attempt"], "no attempt yet"
    return str(view["latest_attempt"]["id"])


async def test_a_timeout_drains_then_kills(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """10: on expiry, SIGTERM, wait the grace, then SIGKILL. S5: --init is what makes
    the SIGTERM land at all, so the worker exits 143 rather than 137 after the grace."""
    url = origin("timeout", "hang")
    register(ctx, "timeout", url)
    document = e2e_contract("E2E-0010", "timeout", worker_image)
    document["execution_request"]["timeout_seconds"] = 15
    task_id = submit_and_start(client, document)

    state = await run_until(supervisor, client, task_id, DONE, max_ticks=80, pause=1.0)
    assert state in DONE
    attempt_id = await _attempt_id(client, task_id)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT exit_class, exit_code, termination_reason, drain_deadline, "
                "logs_drained_at FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).one()
    assert row.exit_class == "timeout"
    assert row.termination_reason == "timeout"
    assert row.drain_deadline is not None
    assert row.exit_code in (143, 137), row.exit_code
    assert row.logs_drained_at is not None
    assert "attempt_timeout_drain" in event_kinds(client, task_id)
    assert daemon.container_ids(f"crucible.attempt={attempt_id}") == []


async def test_a_worker_removed_out_of_band_is_lost(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """10: `lost` only when the daemon cannot see the container."""
    url = origin("loss", "hang")
    register(ctx, "loss", url)
    document = e2e_contract("E2E-0011", "loss", worker_image)
    document["lifecycle"] = {"max_attempts": 1, "retry_on": [], "cleanup": "policy"}
    task_id = submit_and_start(client, document)

    for _ in range(40):
        await supervisor.tick()
        attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            break
        await asyncio.sleep(0.5)
    attempt_id = await _attempt_id(client, task_id)
    containers = daemon.container_ids(f"crucible.attempt={attempt_id}")
    assert containers, "the worker container was never created"
    daemon.rm(*containers)

    await run_until(supervisor, client, task_id, DONE, max_ticks=30, pause=0.5)
    with engine.begin() as conn:
        exit_class = conn.execute(
            text("SELECT exit_class FROM attempts WHERE id = :id"), {"id": attempt_id}
        ).scalar_one()
    assert exit_class == "lost"
    assert "attempt_lost" in event_kinds(client, task_id)


async def test_a_labelled_container_with_no_attempt_row_is_removed(
    supervisor: Supervisor, worker_image: str
) -> None:
    """10 step 3: anything with a Crucible label and no live attempt row is an orphan."""
    name = f"crucible-e2e-orphan-{RUN_ID}"
    daemon.run_detached(
        name,
        [
            "--label",
            "crucible.attempt=01ORPHANORPHANORPHANORPHAN",
            "--label",
            "crucible.task=01ORPHANTASKORPHANTASKORPH",
            "--label",
            "crucible.owner=e2e",
            "--label",
            "crucible.role=worker",
            "--user",
            "1000:1000",
            "--network",
            NET_WORKERS,
            worker_image,
            "sh",
            "-c",
            "while :; do sleep 1; done",
        ],
    )
    try:
        result = await supervisor.tick()
        assert result.orphans >= 1
        assert daemon.container_ids("crucible.attempt=01ORPHANORPHANORPHANORPHAN") == []
    finally:
        daemon.rm(name)


async def test_a_restart_re_attaches_and_resumes_the_log_offset(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    provider: DockerProvider,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """18: Crucible restart with a worker still running; logs resume from the offset."""
    url = origin("restart", "succeed")
    register(ctx, "restart", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0012", "restart", worker_image))

    # First supervisor: launch, then stand down mid-run.
    for _ in range(40):
        await supervisor.tick()
        attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            break
        await asyncio.sleep(0.2)
    attempt_id = await _attempt_id(client, task_id)
    await supervisor.stop()

    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT count(*) FROM log_chunks WHERE attempt_id = :id"), {"id": attempt_id}
        ).scalar_one()

    # A new supervisor, a new provider instance: nothing in memory carries over. It has
    # to find the worker by label (10) and resume the stream by (timestamp, hash).
    fresh_provider = DockerProvider(provider.config)
    successor = Supervisor(
        ctx.uow_factory,
        {"docker": fresh_provider},
        ctx.clock,
        holder=f"e2e-successor-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
    )
    await run_until(successor, client, task_id, DONE, max_ticks=60, pause=0.5)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT content, gzipped, offset_start, offset_end FROM log_chunks "
                "WHERE attempt_id = :id ORDER BY id"
            ),
            {"id": attempt_id},
        ).all()
    assert len(rows) >= before
    body = b"".join(r.content for r in rows if not r.gzipped).decode("utf-8", "replace")
    lines = [line for line in body.splitlines() if "read identity bundle" in line]
    # `docker logs --since` is inclusive (S8): a resume by timestamp alone would
    # repeat the boundary line on every pull.
    assert len(lines) == 1, f"the resume duplicated a line: {lines}"
    offsets = [(r.offset_start, r.offset_end) for r in rows]
    assert all(a[1] == b[0] for a, b in pairwise(offsets))


async def test_the_run_completes_with_no_client_attached(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """18: the API client exits after start; the run completes and a wake is waiting."""
    url = origin("disconnect", "succeed")
    register(ctx, "disconnect", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0013", "disconnect", worker_image))
    client.close()

    for _ in range(60):
        await supervisor.tick()
        with ctx.uow_factory() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None
            if task.state.value in DONE:
                break
        await asyncio.sleep(0.5)
    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None and task.state.value in DONE
        waiting = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=50
        )
    assert waiting, "nothing was waiting for Foundry to poll"


async def test_a_second_attempt_on_the_same_branch_waits_for_the_checkout_lease(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """10: one checkout lease per repository url and work branch."""
    url = origin("lease", "hang")
    register(ctx, "lease", url)
    first = submit_and_start(client, e2e_contract("E2E-0014", "lease", worker_image))
    second_document = e2e_contract("E2E-0015", "lease", worker_image)
    second_document["repository"]["work_branch"] = "crucible/E2E-0014"
    second = submit_and_start(client, second_document)

    for _ in range(20):
        await supervisor.tick()
        view = client.get(f"/v1/tasks/{first}").json()
        if view.get("latest_attempt") and view["latest_attempt"]["state"] == "running":
            break
        await asyncio.sleep(0.2)

    await supervisor.tick()
    blocked = client.get(f"/v1/tasks/{second}").json()
    attempt = blocked.get("latest_attempt")
    assert attempt is None or attempt["state"] in ("pending", "preparing"), attempt
    assert "checkout_lease_denied" in event_kinds(client, second)
    assert "checkout_lease_taken" in event_kinds(client, first)
