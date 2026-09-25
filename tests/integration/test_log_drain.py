"""The final log drain keeps pulling until the log is exhausted (issue 63).

A provider may bound one log pull; the Kubernetes provider reads a few MiB at a time.
The supervisor's final drain before `logs_drained` therefore repeats until a pull brings
nothing, or the end of a worker's log would be the part that is lost."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application import supervisor as supervisor_module
from crucible.ports.execution import Handle, LogChunk, LogOffset
from tests.integration.conftest import make_supervisor, run_to_settled, submit_and_start

pytestmark = pytest.mark.integration


class OneChunkAPull(FakeProvider):
    """A provider whose every log pull returns at most one chunk."""

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        return (await super().logs(h, since))[:1]


def _stored(ctx: AppContext, attempt_id: str) -> list[bytes]:
    assert ctx.engine is not None
    with ctx.engine.begin() as connection:
        rows = connection.execute(
            text("SELECT content FROM log_chunks WHERE attempt_id = :id ORDER BY offset_start"),
            {"id": attempt_id},
        ).scalars()
        return [bytes(row) for row in rows]


async def test_the_final_drain_stores_every_chunk_a_bounded_provider_holds(
    ctx: AppContext, client: TestClient
) -> None:
    provider = OneChunkAPull()
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider._workers[attempt_id]
    assert len(worker.logs) >= 2
    assert _stored(ctx, attempt_id) == [chunk.content for chunk in worker.logs]


async def test_the_final_drain_stops_at_its_cap(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log that outruns the drain does not hold the tick: the attempt moves on with
    what the capped number of pulls stored."""
    monkeypatch.setattr(supervisor_module, "FINAL_DRAIN_PULLS", 1)
    provider = OneChunkAPull()
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    assert len(_stored(ctx, attempt_id)) < len(provider._workers[attempt_id].logs)
