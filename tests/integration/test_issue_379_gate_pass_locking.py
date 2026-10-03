"""Hades #379: the gate pass locks a correction's task only when its pull request is
recorded merged.

The pass runs every tick. It used to take `FOR UPDATE` on every task in a correction
state (scheduled, running, and so on), holding rows the supervisor was about to write
for a pull request that was open all along. Here another transaction holds the lock on
a running correction whose PR is open, and the pass still finishes, settling the other
task whose PR is merged."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.application.supervisor import Supervisor
from crucible.domain.entities import PullRequestState
from tests.integration import test_github_delivery as delivery
from tests.integration.test_github_delivery import publish

app_key = delivery.app_key
github = delivery.github
github_client = delivery.github_client
publisher = delivery.publisher
delivery_supervisor = delivery.delivery_supervisor

pytestmark = pytest.mark.integration


def state(client: TestClient, task_id: str) -> str:
    return str(client.get(f"/v1/tasks/{task_id}").json()["state"])


async def test_the_gate_pass_does_not_wait_on_a_correction_whose_pr_is_open(
    client: TestClient,
    engine: Engine,
    delivery_supervisor: Supervisor,
) -> None:
    open_task, _ = await publish(client, delivery_supervisor, external_id="EX-0001")
    merged_task, _ = await publish(client, delivery_supervisor, external_id="EX-0002")
    # Both are corrections under way against their PRs; only the second PR is merged.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET state = 'running' WHERE id IN (:a, :b)"),
            {"a": open_task, "b": merged_task},
        )
    with delivery_supervisor._fenced() as uow:
        pull_request = uow.pull_requests.get_for_task(merged_task, for_update=True)
        assert pull_request is not None
        pull_request.state = PullRequestState.MERGED
        pull_request.merge_sha = "d" * 40
        uow.pull_requests.save(pull_request)
        uow.commit()

    holder = engine.connect()
    held = holder.begin()
    gate_pass = threading.Thread(target=delivery_supervisor.delivery._evaluate_gates, daemon=True)
    try:
        holder.execute(text("SELECT id FROM tasks WHERE id = :id FOR UPDATE"), {"id": open_task})
        gate_pass.start()
        gate_pass.join(timeout=30)
        finished = not gate_pass.is_alive()
    finally:
        held.rollback()
        holder.close()
        gate_pass.join(timeout=30)

    assert finished, "the gate pass waited on the lock of a correction whose PR is open"
    assert state(client, merged_task) == "merged"
    assert state(client, open_task) == "running"
