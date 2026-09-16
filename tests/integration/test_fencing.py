"""Lease fencing (10): a stale supervisor token's write is rejected by the trigger."""

from __future__ import annotations

from functools import partial

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.auth import mint_token
from crucible.application.supervisor import LeaseLostError
from crucible.application.transitions import record_event
from crucible.domain.entities import (
    AttemptMetrics,
    DispositionKind,
    Event,
    GateResultRecord,
    ReviewDisposition,
    Role,
    Wake,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.ports.repository import FencedTokenRejectedError
from tests.fixtures import FakeClock
from tests.integration.conftest import make_supervisor, run_to_settled, submit_and_start

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


# ----- C2: the new fenced and append-only tables (14) --------------------------


def test_gate_results_require_a_fenced_token(ctx: AppContext) -> None:
    with (
        pytest.raises(FencedTokenRejectedError, match="requires a transaction-local"),
        ctx.uow_factory() as uow,
    ):
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
                head_sha="a" * 40,
                gate="exit_clean",
                phase="pre_pr",
                result="pass",
                detail="written without a token",
                evidence_ids=[],
                evaluated_at=ctx.clock.now(),
            )
        )


def test_attempt_metrics_require_a_fenced_token(ctx: AppContext) -> None:
    with (
        pytest.raises(FencedTokenRejectedError, match="requires a transaction-local"),
        ctx.uow_factory() as uow,
    ):
        uow.attempt_metrics.put(
            AttemptMetrics(
                attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
                task_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                model="gpt-5-codex-mini",
                harness="codex",
                endpoint_kind="subscription",
                pool="openai-sub",
                cost_source="none",
                created_at=ctx.clock.now(),
            )
        )


async def test_a_stale_supervisor_cannot_write_gate_results(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a", lease_ttl_seconds=30)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(a, client, task_id)
    stale = a.fenced_token
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b", lease_ttl_seconds=30)
    assert (await b.tick()).held
    attempt_id = _attempt_id(client, task_id)
    with (
        pytest.raises(FencedTokenRejectedError, match="stale fenced token"),
        ctx.uow_factory() as uow,
    ):
        uow.set_fenced_token(stale or 0)
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id=task_id,
                attempt_id=attempt_id,
                head_sha="a" * 40,
                gate="exit_clean",
                phase="pre_pr",
                result="fail",
                detail="a supervisor that lost the lease says otherwise",
                evidence_ids=[],
                evaluated_at=ctx.clock.now(),
            )
        )


def test_the_api_writes_the_record_tables_without_a_token(ctx: AppContext) -> None:
    """14: the API role writes acceptance_results, decisions, artifacts, and wakes."""
    with ctx.uow_factory() as uow:
        principal = mint_token(uow, ctx.clock, name="api-writer", role=Role.ORCHESTRATOR).principal
        uow.wakes.add(
            Wake(
                id=new_id(),
                principal_id=principal.id,
                task_id=None,
                reason="gates_passed",
                payload={"summary": "written by the API role"},
                created_at=ctx.clock.now(),
            )
        )
        uow.commit()


def test_review_dispositions_are_append_only(ctx: AppContext) -> None:
    with ctx.uow_factory() as uow:
        principal = mint_token(
            uow, ctx.clock, name="disposition-writer", role=Role.ORCHESTRATOR
        ).principal
        disposition = ReviewDisposition(
            id=new_id(),
            review_comment_id="2101",
            principal_id=principal.id,
            disposition=DispositionKind.FIX,
            reasoning="The reviewer is right.",
            created_at=ctx.clock.now(),
        )
        uow.dispositions.add(disposition)
        uow.commit()
    with pytest.raises(Exception, match="append-only"), ctx.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE review_dispositions SET disposition = 'decline' "
                "WHERE review_comment_id = '2101'"
            )
        )


def test_evidence_cannot_claim_a_worker_assertion_is_verified(ctx: AppContext) -> None:
    """A CHECK keeps the 11 rule in the schema, not only in the evaluator."""
    with pytest.raises(IntegrityError), ctx.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO evidence (kind, observed_at, source, verified, payload) "
                "VALUES ('exit_info', now(), 'worker', true, '{}'::jsonb)"
            )
        )
