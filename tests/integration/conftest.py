"""PostgreSQL in a container (testcontainers) or CRUCIBLE_TEST_DATABASE_URL; migrated once,
truncated between tests. The supervisor, API client, and fake provider share one database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.auth import mint_token
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import RepositoryRegistration
from crucible.domain.entities import Role
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document

pytestmark = pytest.mark.integration

# The same digest compose.yaml pins, so the tier and the stack run one Postgres build.
POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)

TRUNCATE = (
    "TRUNCATE idempotency_keys, supervisor_status, completion_claims, leases, events, "
    "attempts, executions, task_contracts, tasks, repositories, principals RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("CRUCIBLE_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated(database_url: str) -> str:
    migrate.downgrade(database_url, "base")
    migrate.upgrade(database_url)
    return database_url


@pytest.fixture
def engine(migrated: str) -> Iterator[Engine]:
    eng = make_engine(migrated)
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE))
    yield eng
    eng.dispose()


@pytest.fixture
def uow_factory(engine: Engine) -> SqlUnitOfWorkFactory:
    return SqlUnitOfWorkFactory(engine)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def ctx(
    engine: Engine,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FakeClock,
    provider: FakeProvider,
    migrated: str,
) -> AppContext:
    return AppContext(
        uow_factory=uow_factory,
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
    )


@pytest.fixture
def tokens(ctx: AppContext) -> dict[str, str]:
    out: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            out[role.value] = mint_token(
                uow, ctx.clock, name=f"{role.value}-principal", role=role
            ).token
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name="example-service",
            registration=RepositoryRegistration(
                url=REPOSITORY_URL, default_branch="main", policy_name="default-software"
            ),
        )
        uow.commit()
    return out


@pytest.fixture
def client(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as c:
        yield c


def make_supervisor(
    ctx: AppContext, provider: FakeProvider, holder: str = "sup-a", **kw: Any
) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        ctx.clock,
        holder=holder,
        lease_ttl_seconds=kw.pop("lease_ttl_seconds", 30),
        grace_seconds=kw.pop("grace_seconds", 60),
        **kw,
    )


@pytest.fixture
def supervisor(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    return make_supervisor(ctx, provider)


def submit_and_start(
    client: TestClient, image: str, external_id: str = "EX-0001", **overrides: Any
) -> str:
    doc = contract_document(external_id=external_id, **overrides)
    doc["repository"]["work_branch"] = f"crucible/{external_id}"
    doc["execution_request"]["image"] = image
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 201, r.text
    task_id: str = r.json()["id"]
    req = doc["execution_request"]
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "harness": req["harness"],
            "model": req["model"],
            "provider": req["provider"],
            "image": req["image"],
            "policy_version": 1,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "scheduled"
    return task_id


async def run_until(
    supervisor: Supervisor, client: TestClient, task_id: str, states: set[str], max_ticks: int = 12
) -> str:
    state = ""
    for _ in range(max_ticks):
        await supervisor.tick()
        state = str(client.get(f"/v1/tasks/{task_id}").json()["state"])
        if state in states:
            return state
    raise AssertionError(f"task never reached {states}; last state {state}")


def event_kinds(client: TestClient, task_id: str) -> list[str]:
    r = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200})
    assert r.status_code == 200
    return [e["kind"] for e in r.json()["items"]]
