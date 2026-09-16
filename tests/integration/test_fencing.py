"""Lease fencing (10): a stale supervisor token's write is rejected by the trigger."""

from __future__ import annotations

from functools import partial

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import LeaseLostError
from crucible.application.transitions import record_event
from crucible.domain.entities import Event
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.ports.repository import FencedTokenRejectedError
from tests.fixtures import FakeClock
from tests.integration.conftest import make_supervisor, submit_and_start

pytestmark = pytest.mark.integration


def _crucible_event(ctx: AppContext) -> Event:
    return Event(
        seq=None,
        ts=ctx.clock.now(),
        kind=EventKind.SUPERVISOR_LEASE_ACQUIRED.value,
        principal=PRINCIPAL_CRUCIBLE,
        verified=True,
        payload={},
    )


async def test_stale_token_write_is_rejected(
    ctx: AppContext, provider: FakeProvider, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a", lease_ttl_seconds=30)
    assert (await a.tick()).held
    stale = a.fenced_token
    assert stale == 1

    # A takes a long pause; B takes over after expiry and gets token 2.
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b", lease_ttl_seconds=30)
    assert (await b.tick()).held
    assert b.fenced_token == 2

    # A resumes and writes with its old token: the database rejects it.
    with (
        pytest.raises(FencedTokenRejectedError, match="stale fenced token 1"),
        ctx.uow_factory() as uow,
    ):
        uow.set_fenced_token(stale)
        uow.events.append(_crucible_event(ctx))

    # And A's own tick notices on renewal and stands down.
    result = await a.tick()
    assert result.held is False and a.fenced_token is None

    # B keeps working.
    assert (await b.tick()).held and b.fenced_token == 2


async def test_missing_token_is_rejected(ctx: AppContext) -> None:
    with (
        pytest.raises(FencedTokenRejectedError, match="requires a transaction-local"),
        ctx.uow_factory() as uow,
    ):
        uow.events.append(_crucible_event(ctx))


def test_api_principal_events_are_not_fenced(ctx: AppContext) -> None:
    with ctx.uow_factory() as uow:
        record_event(uow, ctx.clock, EventKind.PRINCIPAL_CREATED, principal="admin-cli", payload={})
        uow.commit()


async def test_token_is_per_transaction_not_per_connection(
    ctx: AppContext, provider: FakeProvider
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a")
    assert (await a.tick()).held
    with ctx.uow_factory() as uow:
        uow.set_fenced_token(a.fenced_token or 0)
        uow.events.append(_crucible_event(ctx))
        uow.commit()
    # The next transaction on the pool starts without a token.
    with pytest.raises(FencedTokenRejectedError), ctx.uow_factory() as uow:
        uow.events.append(_crucible_event(ctx))


async def test_second_supervisor_waits_as_standby(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a")
    b = make_supervisor(ctx, provider, holder="sup-b")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-3")
    assert (await a.tick()).held
    result = await b.tick()
    assert result.held is False and b.fenced_token is None
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"
    await a.stop()
    assert (await b.tick()).held and b.fenced_token == 2


async def test_stale_supervisor_cannot_renew_attempt_leases_or_status(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a", lease_ttl_seconds=30)
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await a.tick()
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b", lease_ttl_seconds=30)
    assert (await b.tick()).held
    # A still believes it holds token 1 and tries the non-fenced writes directly.
    with pytest.raises(LeaseLostError):
        await a._db(partial(a._renew_attempt_lease, _attempt_id(client, task_id)))
    assert a.fenced_token is None
    with pytest.raises(FencedTokenRejectedError), ctx.uow_factory() as uow:
        uow.set_fenced_token(1)
        status = uow.supervisor_status.get()
        status.holder = "sup-a"
        uow.supervisor_status.write(status)
    assert client.get("/v1/supervisor").json()["lease"]["holder"] == "sup-b"


def _attempt_id(client: TestClient, task_id: str) -> str:
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    return str(attempt["id"])


def test_reserved_principal_names(ctx: AppContext) -> None:
    from crucible.application.auth import mint_token  # noqa: PLC0415
    from crucible.domain.entities import Role  # noqa: PLC0415

    with ctx.uow_factory() as uow:
        for name in ("crucible", "worker:abc", " "):
            with pytest.raises(ValueError, match="reserved"):
                mint_token(uow, ctx.clock, name=name, role=Role.OBSERVER)


async def test_same_holder_name_gets_a_new_token(
    ctx: AppContext, provider: FakeProvider, clock: FakeClock
) -> None:
    a1 = make_supervisor(ctx, provider, holder="same")
    assert (await a1.tick()).held and a1.fenced_token == 1
    clock.advance(31)
    a2 = make_supervisor(ctx, provider, holder="same")
    assert (await a2.tick()).held and a2.fenced_token == 2
    assert (await a1.tick()).held is False
