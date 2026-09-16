"""Readiness must not say healthy next to a dead supervisor or a drifted schema."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.persistence import migrate
from crucible.application.supervisor import Supervisor
from tests.fixtures import FakeClock
from tests.integration.conftest import run_to_settled, submit_and_start

pytestmark = pytest.mark.integration


async def test_ready_turns_not_ready_when_the_tick_fails(
    client: TestClient, supervisor: Supervisor, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await supervisor.tick()
    ready = client.get("/v1/ready")
    assert ready.status_code == 200 and ready.json()["supervisor"]["ok"] is True

    def boom() -> None:
        raise RuntimeError("UndefinedColumn attempts.killed_at")

    monkeypatch.setattr(supervisor, "_materialize_scheduled", boom)
    clock.advance(5)
    with pytest.raises(RuntimeError):
        await supervisor.tick()
    ready = client.get("/v1/ready")
    assert ready.status_code == 503
    detail = ready.json()["supervisor"]["detail"]
    assert ready.json()["supervisor"]["ok"] is False
    assert detail == "last tick failed: RuntimeError: UndefinedColumn attempts.killed_at"
    assert ready.json()["database"]["ok"] and ready.json()["migrations"]["ok"]
    sup = client.get("/v1/supervisor").json()
    assert sup["healthy"] is False and "UndefinedColumn" in sup["last_error"]
    assert sup["lease"]["holder"] == "sup-a", "the lease is still held; that alone is not health"
    assert client.get("/v1/health").status_code == 200

    # The lease keeps being renewed by failing ticks; past the window the detail says so.
    clock.advance(31)
    with pytest.raises(RuntimeError):
        await supervisor.tick()
    detail = client.get("/v1/ready").json()["supervisor"]["detail"]
    assert detail.startswith("no successful tick within the lease window; last error:")

    monkeypatch.undo()
    clock.advance(5)
    await supervisor.tick()
    ready = client.get("/v1/ready")
    assert ready.status_code == 200
    assert "last successful tick" in ready.json()["supervisor"]["detail"]
    sup = client.get("/v1/supervisor").json()
    assert sup["healthy"] is True and sup["last_error"] is not None, "history is kept"


async def test_ready_turns_not_ready_when_a_task_cannot_progress(
    client: TestClient, supervisor: Supervisor, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure mode: a task stays scheduled forever while readiness is green."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    monkeypatch.setattr(
        supervisor, "_materialize_scheduled", lambda: (_ for _ in ()).throw(RuntimeError("dead"))
    )
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await supervisor.tick()
        clock.advance(5)
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "scheduled"
    assert client.get("/v1/ready").status_code == 503
    monkeypatch.undo()
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"


def test_ready_reports_schema_drift(client: TestClient, engine: Engine, migrated: str) -> None:
    assert client.get("/v1/ready").json()["migrations"]["ok"] is True
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE attempts DROP COLUMN killed_at"))
    try:
        ready = client.get("/v1/ready")
        assert ready.status_code == 503
        detail = ready.json()["migrations"]["detail"]
        head = migrate.head_revision(migrated)
        assert detail.startswith(f"schema drift at head {head}: add_column attempts.killed_at")
        ok, serve_detail = migrate.is_current(engine, migrated)
        assert not ok and "schema drift" in serve_detail, "serve refuses on the same check"
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE attempts ADD COLUMN killed_at timestamptz"))
    assert client.get("/v1/ready").json()["migrations"]["ok"] is True


def test_ready_reports_a_missing_table(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE idempotency_keys RENAME TO idempotency_keys_old"))
    try:
        detail = client.get("/v1/ready").json()["migrations"]["detail"]
        assert "schema drift" in detail and "idempotency_keys" in detail
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE idempotency_keys_old RENAME TO idempotency_keys"))
