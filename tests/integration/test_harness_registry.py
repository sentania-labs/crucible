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
    _disable(ctx, "codex", "rotating the dedicated credential")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
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
    assert codex["capabilities"]["endpoints"] == ["api.openai.com", "auth.openai.com"]
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
    assert await run_to_settled(supervisor, client, second, max_ticks=40) == (
        "awaiting_internal_review"
    )
