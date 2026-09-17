"""The lifecycle through the registry on the fake provider (07, 18, 25): a disabled
harness is refused at launch with a wake and never retried; enabling it through the
service lets the same contract run; `GET /harnesses` and `GET /images` report the
sanitized state; per-harness concurrency defers the second launch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.harnesses import set_harness_enabled
from crucible.application.supervisor import Supervisor
from tests.integration.conftest import (
    event_kinds,
    make_supervisor,
    run_to_settled,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def _disable(ctx: AppContext, name: str, reason: str) -> None:
    with ctx.uow_factory() as uow:
        set_harness_enabled(
            uow,
            ctx.clock,
            principal_name="admin-principal",
            name=name,
            enabled=False,
            reason=reason,
        )
        uow.commit()


async def test_a_disabled_harness_is_refused_with_a_wake_and_no_retry(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    # Disabled after submit: at submit the gate is a contract problem (review I8); a
    # harness disabled while a task is scheduled is what the launch-time refusal is for.
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    _disable(ctx, "codex", "rotating the dedicated credential")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    kinds = event_kinds(client, task_id)
    assert "harness_refused" in kinds
    assert "task_retry_scheduled" not in kinds, "a refusal is not retried (07)"
    assert kinds.count("attempt_created") == 1
    refused = next(
        e
        for e in client.get(f"/v1/tasks/{task_id}/events").json()["items"]
        if e["kind"] == "harness_refused"
    )
    assert refused["payload"]["harness"] == "codex"
    assert (
        "disabled by an administrator: rotating the dedicated credential"
        in refused["payload"]["detail"]
    )
    wakes = client.get("/v1/wakes").json()["items"]
    unavailable = [w for w in wakes if w["reason"] == "harness_unavailable"]
    assert len(unavailable) == 1 and "codex" in unavailable[0]["summary"]
    view = client.get(f"/v1/tasks/{task_id}").json()
    (attempt,) = [a for e in view["executions"] for a in e["attempts"]]
    assert attempt["exit_class"] == "environment"


async def test_enabling_through_the_service_lets_the_same_harness_run(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    _disable(ctx, "codex", "paused")
    with ctx.uow_factory() as uow:
        state = set_harness_enabled(
            uow,
            ctx.clock,
            principal_name="admin-principal",
            name="codex",
            enabled=True,
            reason="credential validated",
        )
        uow.commit()
    assert state.enabled and state.reason == "credential validated"
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    kinds = event_kinds(client, task_id)
    assert "harness_refused" not in kinds


async def test_a_reason_is_required_to_change_the_flag(ctx: AppContext) -> None:
    with ctx.uow_factory() as uow, pytest.raises(ValueError, match="reason is required"):
        set_harness_enabled(
            uow, ctx.clock, principal_name="admin-principal", name="agy", enabled=False, reason="  "
        )


async def test_get_harnesses_reports_flags_ranges_and_a_sanitized_credential_state(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    _disable(ctx, "agy", "unverified: waiting on the Crucible-side refresh (S1b)")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    items = {h["name"]: h for h in client.get("/v1/harnesses").json()["items"]}
    assert set(items) == {"claude_code", "codex", "agy", "script-harness"}
    agy = items["agy"]
    assert agy["enabled"] is False and agy["enabled_by_administrator"] is False
    assert agy["enabled_by_configuration"] is True
    assert "unverified" in agy["reason"]
    assert agy["supported_versions"] == ">=1.2.0,<1.3.0"
    # No credential path is configured in this tier: absent, and nothing else to say.
    assert agy["credential"]["state"] == "absent"
    assert agy["credential"]["source_fingerprint"] is None
    codex = items["codex"]
    assert codex["enabled"] is True
    assert codex["credential"]["last_launch_outcome"] == "completed"
    assert codex["credential"]["last_launch_at"] is not None
    assert codex["capabilities"]["endpoints"] == [
        "api.openai.com",
        "auth.openai.com",
        "chatgpt.com",
    ]
    # The fake provider lists no images (08); the endpoint still answers.
    assert client.get("/v1/images").json()["items"] == []
    # Nothing secret-shaped anywhere in the two documents.
    blob = client.get("/v1/harnesses").text
    assert "token" not in blob.lower().replace("oauth-token", "").replace("oauth_token", "")


async def test_per_harness_concurrency_defers_the_second_launch(
    ctx: AppContext, client: TestClient, provider: FakeProvider
) -> None:
    """05b and 12: concurrency 1 for a harness whose credential is rw-narrow."""
    supervisor = make_supervisor(ctx, provider)
    first = submit_and_start(client, "crucible-worker:fake-hang", external_id="EX-0001")
    second = submit_and_start(client, "crucible-worker:fake-succeed", external_id="EX-0002")
    await supervisor.tick()
    await supervisor.tick()
    states = {task: client.get(f"/v1/tasks/{task}").json()["state"] for task in (first, second)}
    assert states[first] == "running"
    assert states[second] == "scheduled", states
    assert "harness_launch_deferred" in event_kinds(client, second)
    # Deferred once per attempt, not once per tick.
    await supervisor.tick()
    assert event_kinds(client, second).count("harness_launch_deferred") == 1
    # Cancel the hang; the second launches on a later tick.
    cancel = client.post(
        f"/v1/tasks/{first}/cancel",
        json={"reason": "free the harness", "verbatim": "cancel it", "decided_by": "tests"},
    )
    assert cancel.status_code == 200, cancel.text
    # The hang ignores the drain; past the grace it is killed, exits, and is collected,
    # and only then does the cap release the second launch (review I2).
    await supervisor.tick()
    ctx.clock.advance(61)  # type: ignore[attr-defined]
    for _ in range(3):
        await supervisor.tick()
    assert await run_to_settled(supervisor, client, second, max_ticks=40) == (
        "awaiting_internal_review"
    )


async def test_a_disabled_harness_is_a_contract_problem_at_submit(
    ctx: AppContext, client: TestClient
) -> None:
    """25 (review I8): refused with the reason when the contract is submitted, not a
    task later at launch."""
    _disable(ctx, "codex", "rotating the dedicated credential")
    from tests.fixtures import contract_document  # noqa: PLC0415

    response = client.post("/v1/tasks", json=contract_document())
    assert response.status_code == 422, response.text
    problems = response.json()["errors"]
    assert any(
        p["path"] == "execution_request.harness" and "rotating" in p["message"] for p in problems
    ), problems


async def test_the_cap_holds_until_the_copy_is_synced_and_removed(
    ctx: AppContext, client: TestClient, provider: FakeProvider
) -> None:
    """12 (review I2): an attempt in terminating or exited still holds its credential
    copy, so a second seeding must wait for collect."""
    from crucible.domain.lifecycle import AttemptState  # noqa: PLC0415

    supervisor = make_supervisor(ctx, provider)
    first = submit_and_start(client, "crucible-worker:fake-hang", external_id="EX-0001")
    await supervisor.tick()
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{first}").json()
    attempt_id = view["latest_attempt"]["id"]
    assert view["latest_attempt"]["state"] == "running"
    with ctx.uow_factory() as uow:
        execution = uow.executions.get(view["executions"][0]["id"])
        assert execution is not None
        assert supervisor._harness_busy(execution) is not None
    for state in (AttemptState.TERMINATING, AttemptState.EXITED):
        with supervisor._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.state = state
            uow.attempts.save(attempt)
            uow.commit()
        with ctx.uow_factory() as uow:
            assert supervisor._harness_busy(execution) is not None, state
    with supervisor._fenced() as uow:
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert attempt is not None
        attempt.state = AttemptState.COLLECTED
        uow.attempts.save(attempt)
        uow.commit()
    with ctx.uow_factory() as uow:
        assert supervisor._harness_busy(execution) is None


async def test_a_lost_worker_has_its_credential_copy_discarded(
    ctx: AppContext, client: TestClient, provider: FakeProvider, supervisor: Supervisor
) -> None:
    """12 (review I1): a lost worker is never collected, so the provider is told to
    discard its copy."""
    task_id = submit_and_start(client, "crucible-worker:fake-vanish-1")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"][0]["id"]
    assert attempt_id in provider.discarded


async def test_a_token_shaped_worker_log_line_is_stored_redacted(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    """12 (review I5): provider log capture passes through the redaction filter."""
    from sqlalchemy import text  # noqa: PLC0415

    from crucible.ports.execution import LogChunk  # noqa: PLC0415

    token = "sk-ant-" + "oat01-" + "x" * 40
    line = f"auth: using {token} for the session\n".encode()
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    stored = supervisor._store_logs(attempt_id, (LogChunk("stdout", line, lines=1),))
    assert stored == 1
    with ctx.engine.begin() as connection:
        rows = (
            connection.execute(
                text("SELECT content FROM log_chunks WHERE attempt_id = :id"), {"id": attempt_id}
            )
            .scalars()
            .all()
        )
    blob = b"".join(bytes(r) for r in rows).decode("utf-8", "replace")
    assert token not in blob
    assert "[redacted:anthropic_oauth_token]" in blob
