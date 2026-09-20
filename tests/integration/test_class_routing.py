from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ImagePromotion, Policy, RoutingPolicyRecord
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import make_supervisor

pytestmark = pytest.mark.integration


def _model(model: str, harness: str, pool: str) -> dict[str, Any]:
    return {
        "id": model,
        "harness": harness,
        "endpoint": "subscription",
        "capability": "mid",
        "cost": "low",
        "speed": "fast",
        "pool": pool,
        "weight": 1,
        "enabled": True,
    }


def _install_policy(
    ctx: AppContext,
    clock: FakeClock,
    *,
    version: int,
    models: list[dict[str, Any]],
    reroute_max: int = 3,
    wait_max: int = 60,
    cooldown: int = 30,
) -> None:
    with ctx.uow_factory() as uow:
        seeded = uow.policies.get("default-software", 3)
        assert seeded is not None
        routing = {
            "schema_version": "1.0",
            "name": "class-routing-test",
            "version": version,
            "tiers": {
                "trivial": {"allowed_capability": ["mid"], "prefer": ["mid"]},
                "standard": {"allowed_capability": ["mid"], "prefer": ["mid"]},
                "complex": {"allowed_capability": ["mid"], "prefer": ["mid"]},
            },
            "models": models,
            "pools": {
                model["pool"]: {
                    "window": "1h",
                    "budget_units": "attempts",
                    "soft_limit": 0,
                    "default_cooldown_seconds": cooldown,
                }
                for model in models
            },
            "rotation": {
                "strategy": "weighted-least-recent",
                "quality_feedback": True,
                "quality_window": 20,
            },
            "reroute": {
                "reroute_max": reroute_max,
                "resume_max_wait_seconds": wait_max,
            },
        }
        policy = copy.deepcopy(seeded.document)
        policy["version"] = version
        policy["routing"] = {"policy": {"name": "class-routing-test", "version": version}}
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="class-routing-test",
                version=version,
                document=routing,
                created_at=clock.now(),
            )
        )
        uow.policies.put(
            Policy(
                name="default-software",
                version=version,
                document=policy,
                created_at=clock.now(),
            )
        )
        uow.commit()


def _promote(ctx: AppContext, clock: FakeClock, harness: str, image: str) -> None:
    with ctx.uow_factory() as uow:
        for existing in uow.image_promotions.list_all():
            if existing.harness == harness and existing.state == "default":
                existing.state = "retained"
                uow.image_promotions.put(existing)
        uow.image_promotions.put(
            ImagePromotion(
                digest=f"sha256:{harness}-{image}",
                reference=image,
                harness=harness,
                harness_version="1.0.0",
                state="default",
                updated_at=clock.now(),
                updated_by="tests",
                reason="class routing integration fixture",
            )
        )
        uow.commit()


def _class_contract(external_id: str, version: int) -> dict[str, Any]:
    document = contract_document(external_id=external_id)
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["policy"] = {"name": "default-software", "version": version}
    for field in ("harness", "model", "pin_reason", "image"):
        document["execution_request"].pop(field, None)
    return document


def _submit(client: TestClient, external_id: str, version: int) -> str:
    response = client.post("/v1/tasks", json=_class_contract(external_id, version))
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={"provider": "fake", "policy_version": version},
    )
    assert response.status_code == 200, response.text
    return task_id


def _events(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"])


async def test_quota_exit_commits_wip_marks_pool_and_reroutes_to_another_pool(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    _install_policy(
        ctx,
        clock,
        version=80,
        models=[
            _model("a-quota-model", "codex", "pool-a"),
            _model("b-success-model", "agy", "pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-REROUTE", 80)

    await supervisor.tick()
    launched = client.get(f"/v1/tasks/{task_id}").json()
    first_attempt = launched["executions"][0]["attempts"][0]
    assert first_attempt["model"] == "a-quota-model", first_attempt
    assert first_attempt["image"] == "crucible-worker:fake-quota", first_attempt
    midway = launched
    assert midway["state"] == "scheduled"
    assert [attempt["pool"] for attempt in midway["executions"][0]["attempts"]] == [
        "pool-a",
        "pool-b",
    ]
    assert midway["executions"][0]["attempts"][1]["resume_from_remote"] is True

    events = _events(client, task_id)
    reroute = next(event for event in events if event["kind"] == "task_rerouted")
    assert reroute["payload"]["from_pool"] == "pool-a"
    assert reroute["payload"]["model"] == "b-success-model"
    assert reroute["payload"]["wip_commit_sha"]
    assert any(event["kind"] == "quota_wip_committed" for event in events)
    usage = client.get("/v1/routing/usage", params={"policy_version": 80}).json()
    pool_a = next(pool for pool in usage["pools"] if pool["pool"] == "pool-a")
    assert pool_a["exhausted_until"] is not None

    assert ctx.harnesses is not None
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
    )
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}
    ) as admin:
        listed = admin.get("/v1/admin/routing/exhaustion").json()["items"]
        assert next(mark for mark in listed if mark["pool"] == "pool-a")["active"] is True
        cleared = admin.post(
            "/v1/admin/routing/exhaustion/pool-a/clear",
            json={"reason": "integration parity"},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["active"] is False
        audit = admin.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        assert any(event["kind"] == "pool_exhaustion_cleared" for event in audit)

    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    attempts = final["executions"][0]["attempts"]
    assert [(item["model"], item["harness"]) for item in attempts] == [
        ("a-quota-model", "codex"),
        ("b-success-model", "agy"),
    ]
    assert attempts[1]["state"] == "succeeded"


async def test_all_pools_wait_and_a_restarted_supervisor_resumes_on_schedule(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=81,
        models=[_model("only-model", "codex", "only-pool")],
        wait_max=60,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-WAIT", 81)
    await supervisor.tick()
    await supervisor.tick()

    waiting = client.get(f"/v1/tasks/{task_id}").json()
    assert waiting["state"] == "awaiting_quota"
    assert datetime.fromisoformat(waiting["resume_at"]) == clock.now() + timedelta(seconds=30)
    assert waiting["resume_at"] is not None
    assert sum(event["kind"] == "wake_created" for event in _events(client, task_id)) == 1

    await supervisor.stop()
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    clock.advance(31)
    restarted = make_supervisor(ctx, provider, holder="sup-restarted")
    await restarted.tick()
    await restarted.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["resume_at"] is None
    assert len(view["executions"][0]["attempts"]) == 2
    assert any(event["kind"] == "task_quota_resumed" for event in _events(client, task_id))
    await restarted.stop()


async def test_reroute_and_wait_caps_end_through_the_reported_path(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=82,
        models=[
            _model("a-quota-model", "codex", "cap-pool-a"),
            _model("b-model", "agy", "cap-pool-b"),
        ],
        reroute_max=0,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-REROUTE-CAP", 82)
    await supervisor.tick()
    await supervisor.tick()
    kinds = [event["kind"] for event in _events(client, task_id)]
    assert "task_rerouted" not in kinds
    assert "task_reported" in kinds


async def test_two_supervisors_cannot_double_launch_a_class_selected_attempt(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=83,
        models=[_model("selected-model", "codex", "selected-pool")],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-FENCE", 83)
    other = make_supervisor(ctx, provider, holder="sup-other")
    first = await supervisor.tick()
    second = await other.tick()
    assert first.held is True and first.launched == 1
    assert second.held is False and second.launched == 0
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert len(view["executions"][0]["attempts"]) == 1
    await supervisor.stop()


async def test_wait_cap_ends_the_task_through_reported_with_the_class_visible(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=84,
        models=[_model("wait-model", "codex", "wait-pool")],
        wait_max=10,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-WAIT-CAP", 84)
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_quota"

    clock.advance(11)
    await supervisor.tick()
    events = _events(client, task_id)
    reported = next(event for event in events if event["kind"] == "task_reported")
    assert reported["payload"]["exit_class"] == "quota_exhausted"
    assert reported["payload"]["tier"] == "standard"
    assert any(
        event["kind"] == "execution_failed" and event["payload"].get("wait_cap_exceeded") is True
        for event in events
    )
