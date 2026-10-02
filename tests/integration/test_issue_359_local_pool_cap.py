"""Attempt route persistence and recovery use PostgreSQL, including the migration."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.persistence import migrate
from crucible.application.supervisor import Supervisor
from crucible.domain.lifecycle import AttemptState
from tests.fixtures import FakeClock
from tests.integration.test_class_routing import _install_policy, _model, _submit

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("harness", ["codex", "hermes"])
async def test_attempt_route_round_trip_and_migration_recovery(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    database_url: str,
    harness: str,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=80,
        models=[
            {
                **_model("local-model", harness, "lab-local"),
                "endpoint": "local",
                "endpoint_url": "http://gateway.test/v1",
            }
        ],
    )
    # Submit without a local launch: this test exercises repository persistence and
    # migration recovery; the unit regression exercises the launch and cap paths.
    task_id = _submit(client, "POOL-PERSIST", 3)
    await supervisor.tick()
    with ctx.uow_factory() as uow:
        attempt = uow.attempts.list_for_task(task_id)[0]
        execution = uow.executions.get(attempt.execution_id)
        assert execution is not None
        execution = replace(execution, id="persisted-route-execution")
        execution.model = "local-model"
        execution.harness = harness
        execution.policy_snapshot = {
            "routing": {"policy": {"name": "class-routing-test", "version": 80}}
        }
        uow.executions.add(execution)
        attempt = replace(attempt, id="persisted-route-attempt", execution_id=execution.id)
        attempt.state = AttemptState.RUNNING
        attempt.selected_model = "local-model"
        attempt.selected_harness = harness
        attempt.selected_pool = "lab-local"
        attempt.routing_version = 79
        uow.attempts.add(attempt)
        attempt.routing_version = 80
        uow.attempts.save(attempt)
        pending = replace(attempt, id="pending-route", number=2, state=AttemptState.PENDING)
        pending.selected_pool = None
        pending.routing_version = None
        uow.attempts.add(pending)
        uow.commit()
    with ctx.uow_factory() as uow:
        saved = uow.attempts.get(attempt.id)
        assert saved is not None
        assert (saved.selected_pool, saved.routing_version) == ("lab-local", 80)
        saved = uow.attempts.get(pending.id)
        assert saved is not None and saved.routing_version is None
    assert ctx.engine is not None
    try:
        migrate.downgrade(database_url, "0035_credential_renewer")
        with ctx.engine.begin() as conn:
            conn.execute(
                text("UPDATE attempts SET selected_pool = NULL WHERE id = :id"),
                {"id": attempt.id},
            )
        migrate.upgrade(database_url)
        with ctx.uow_factory() as uow:
            saved = uow.attempts.get(attempt.id)
            assert saved is not None
            assert (saved.selected_pool, saved.routing_version) == ("lab-local", 80)
            saved = uow.attempts.get(pending.id)
            assert saved is not None
            assert (saved.selected_pool, saved.routing_version) == (None, None)
        current, detail = migrate.is_current(ctx.engine, database_url)
        assert current, detail
    finally:
        migrate.upgrade(database_url)
