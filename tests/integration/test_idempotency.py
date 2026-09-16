"""Idempotency keys: one transaction with the mutation, replay, and concurrency (04)."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWork
from tests.fixtures import contract_document

pytestmark = pytest.mark.integration


def test_mutation_and_key_commit_in_one_transaction(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits: list[int] = []
    original = SqlUnitOfWork.commit

    def counting_commit(self: SqlUnitOfWork) -> None:
        commits.append(1)
        original(self)

    monkeypatch.setattr(SqlUnitOfWork, "commit", counting_commit)
    r = client.post("/v1/tasks", json=contract_document(), headers={"Idempotency-Key": "one"})
    assert r.status_code == 201
    assert len(commits) == 1, "the task row and the key row are one commit"
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT response_status, response_body->>'id' FROM idempotency_keys")
        ).one()
        assert row[0] == 201 and row[1] == r.json()["id"]
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 1


def test_failed_mutation_leaves_no_key_and_records_the_rejection(
    client: TestClient, engine: Engine
) -> None:
    bad = contract_document()
    bad["repository"]["name"] = "not-registered"
    r = client.post("/v1/tasks", json=bad, headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and r.json()["type"] == "urn:crucible:problem:contract-invalid"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 0
    rejected = client.get("/v1/events", params={"kind": "contract_rejected"}).json()["items"]
    assert len(rejected) == 1 and rejected[0]["principal"] == "orchestrator-principal"
    # The key is free again for the corrected request.
    r = client.post("/v1/tasks", json=contract_document(), headers={"Idempotency-Key": "k"})
    assert r.status_code == 201


def test_same_key_retry_returns_the_original(client: TestClient) -> None:
    doc = contract_document()
    first = client.post("/v1/tasks", json=doc, headers={"Idempotency-Key": "retry"})
    second = client.post("/v1/tasks", json=doc, headers={"Idempotency-Key": "retry"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert len(client.get("/v1/tasks").json()["items"]) == 1


def test_concurrent_first_requests_execute_the_mutation_once(
    ctx: AppContext, tokens: dict[str, str], engine: Engine
) -> None:
    app = create_app(ctx)
    headers = {"Authorization": f"Bearer {tokens['orchestrator']}", "Idempotency-Key": "race"}
    doc = contract_document()
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict[str, object], str | None]] = []
    lock = threading.Lock()

    def submit() -> None:
        with TestClient(app, headers=headers) as c:
            barrier.wait()
            r = c.post("/v1/tasks", json=doc)
            with lock:
                results.append((r.status_code, r.json(), r.headers.get("idempotent-replayed")))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    statuses = sorted(s for s, _, _ in results)
    assert statuses in ([201, 201], [201, 409]), results
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() == 1
    if statuses == [201, 201]:
        ids = {body["id"] for _, body, _ in results}
        assert len(ids) == 1
        assert sorted(replayed or "" for _, _, replayed in results) == ["", "true"]
