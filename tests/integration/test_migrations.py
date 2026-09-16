from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import make_engine

pytestmark = pytest.mark.integration


def test_up_down_up_from_empty(database_url: str) -> None:
    migrate.downgrade(database_url, "base")
    engine = make_engine(database_url)
    assert (
        inspect(engine).get_table_names() == ["alembic_version"]
        or "tasks" not in inspect(engine).get_table_names()
    )
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert {
        "principals",
        "repositories",
        "policies",
        "tasks",
        "task_contracts",
        "executions",
        "attempts",
        "events",
        "leases",
        "supervisor_status",
        "completion_claims",
    } <= names
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM policies")).scalar() == 1
    migrate.downgrade(database_url, "base")
    assert "tasks" not in inspect(engine).get_table_names()
    migrate.upgrade(database_url)
    engine.dispose()


def test_events_and_contracts_are_append_only(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'principal_created', 'tests', true, '{}')"
            )
        )
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("UPDATE events SET kind = 'task_submitted'"))
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM events"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == 1
    engine.dispose()


def test_unknown_event_kind_is_rejected(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.begin() as conn, pytest.raises(Exception, match="ck_events_kind"):
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'made_up_kind', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_fresh_schema_has_no_drift(migrated: str) -> None:
    engine = make_engine(migrated)
    assert migrate.schema_drift(engine) is None
    ok, detail = migrate.is_current(engine, migrated)
    assert ok and "schema matches" in detail
    engine.dispose()


def test_0002_down_and_up(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0001_walking_skeleton")
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert "last_success_at" not in cols
    migrate.upgrade(database_url)
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert {"last_success_at", "last_error_at", "last_error"} <= cols
    engine.dispose()
