"""Hades #359: 0036 adds the routing policy name and version to attempts and backfills
existing rows from their execution's policy snapshot, with the fenced trigger back on."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import make_engine
from crucible.application.supervisor import Supervisor
from tests.integration.conftest import rebuild, reset, submit_and_start

pytestmark = pytest.mark.integration

PREVIOUS = "0035_credential_renewer"
COLUMNS = {"routing_version"}


@pytest.fixture(autouse=True)
def at_clean_head(migrated: str) -> Iterator[None]:
    """The test leaves the database at head with every table reset, as the migration
    tests do, so a failure halfway down does not break the tests after it."""
    yield
    try:
        migrate.upgrade(migrated)
    except Exception:
        rebuild(migrated)
    engine = make_engine(migrated)
    try:
        reset(engine)
    finally:
        engine.dispose()


def _attempt_columns(url: str) -> set[str]:
    engine = make_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("attempts")}
    finally:
        engine.dispose()


async def test_0036_backfills_existing_attempts_from_the_execution_snapshot(
    client: TestClient, supervisor: Supervisor, migrated: str
) -> None:
    routed_task = submit_and_start(client, "crucible-worker:fake-succeed", "MIG-0359-A")
    bare_task = submit_and_start(client, "crucible-worker:fake-succeed", "MIG-0359-B")
    await supervisor.tick()

    migrate.downgrade(migrated, PREVIOUS)
    assert not COLUMNS & _attempt_columns(migrated)
    engine = make_engine(migrated)
    try:
        with engine.begin() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM attempts WHERE task_id IN (:a, :b)"),
                {"a": routed_task, "b": bare_task},
            ).scalar_one()
            assert count >= 2
            # An execution whose policy carries no routing reference: its attempts stay
            # null rather than failing the upgrade.
            conn.execute(text("ALTER TABLE executions DISABLE TRIGGER trg_executions_fenced"))
            conn.execute(
                text(
                    "UPDATE executions SET policy_snapshot = policy_snapshot - 'routing' "
                    "WHERE task_id = :task"
                ),
                {"task": bare_task},
            )
            conn.execute(text("ALTER TABLE executions ENABLE TRIGGER trg_executions_fenced"))

        migrate.upgrade(migrated)
        assert _attempt_columns(migrated) >= COLUMNS

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.task_id, a.routing_version, "
                    "x.policy_snapshot->'routing'->'policy'->>'name' AS snapshot_name, "
                    "x.policy_snapshot->'routing'->'policy'->>'version' AS snapshot_version "
                    "FROM attempts a JOIN executions x ON x.id = a.execution_id "
                    "WHERE a.task_id IN (:a, :b)"
                ),
                {"a": routed_task, "b": bare_task},
            ).all()
            routed = [row for row in rows if row.task_id == routed_task]
            bare = [row for row in rows if row.task_id == bare_task]
            assert routed and bare
            for row in routed:
                assert row.snapshot_name is not None
                assert row.routing_policy_name == row.snapshot_name
                assert row.routing_version == int(row.snapshot_version)
            assert all(row.routing_version is None for row in bare)
            # The fenced trigger is back on after the backfill.
            enabled = conn.execute(
                text(
                    "SELECT tgenabled FROM pg_trigger "
                    "WHERE tgname = 'trg_attempts_fenced' "
                    "AND tgrelid = 'attempts'::regclass"
                )
            ).scalar_one()
            assert enabled == "O"
        with engine.begin() as conn, pytest.raises(Exception, match="fenced_token"):
            conn.execute(
                text("UPDATE attempts SET routing_version = 0 WHERE task_id = :task"),
                {"task": routed_task},
            )
        assert migrate.schema_drift(engine) is None
    finally:
        engine.dispose()
