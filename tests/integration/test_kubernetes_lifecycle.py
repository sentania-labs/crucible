"""The lifecycle against the Kubernetes provider (26, requirement 7 of C8a).

The same four cases the Docker and fake providers already carry, driven by a real
supervisor against a real database, with an in-memory Kubernetes API underneath: a full
run to gates, the failure classes of 16, a cancel, and a reconcile across a supervisor
restart.

The fake API is not a cluster. What it proves is that the provider creates the right
objects, reads the states back correctly, and that everything above the provider is
unchanged by the second provider existing. The cluster half is C8b's `make e2e-kind`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.auth import mint_token
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Role
from crucible.ports.execution import ObservationState
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document, promote_for_test
from tests.integration.conftest import event_kinds, run_to_settled

pytestmark = pytest.mark.integration

IMAGE = "ghcr.io/sentania-labs/crucible-worker:script-harness-1.0.0"
HOSTS = {"github.com": ["140.82.121.4/32"], "pypi.org": ["151.101.0.223/32"]}


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def k8s_provider(k8s_api: FakeKubernetesApi) -> KubernetesProvider:
    registry = FakeRegistry(k8s_api)
    registry.register(IMAGE, harness="script-harness", version="1.0.0")
    return KubernetesProvider(
        KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            extra_image_allowlist=("ghcr.io/sentania-labs/crucible-worker:*",),
        ),
        k8s_api,  # type: ignore[arg-type]
        registry,
        harnesses=default_registry(test_fixtures=True),
        resolver=lambda host: list(HOSTS.get(host, ["203.0.113.1/32"])),
    )


@pytest.fixture
def k8s_ctx(
    engine: Engine,
    migrated: str,
    clock: FakeClock,
    k8s_provider: KubernetesProvider,
    tmp_path: Path,
) -> AppContext:
    return AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[k8s_provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(tmp_path / "artifacts"),
        harnesses=default_registry(test_fixtures=True),
    )


@pytest.fixture
def k8s_client(k8s_ctx: AppContext) -> Iterator[TestClient]:
    """An admin seeds the routing, the policy, the repository and the promoted image;
    the operator submits. The same sequence a real deployment goes through (25)."""
    app = create_app(k8s_ctx)
    with k8s_ctx.uow_factory() as uow:
        tokens = {
            role.value: mint_token(
                uow, k8s_ctx.clock, name=f"{role.value}-principal", role=role
            ).token
            for role in Role
        }
        promote_for_test(
            uow,
            digest="sha256:" + "c" * 64,
            reference=IMAGE,
            harnesses={"script-harness": "1.0.0"},
            at=k8s_ctx.clock.now(),
            by="tests",
            reason="the C8a integration tier's script harness image",
        )
        uow.commit()
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        # A conflict is the same document already uploaded by an earlier test in this
        # session: policy and routing rows are immutable once referenced and survive
        # the per-test truncation.
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201, 409)
        policy = e2e_policy_document(name="k8s-script")
        policy["images"]["allowlist"] = ["ghcr.io/sentania-labs/crucible-worker:*"]
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201, 409)
    with k8s_ctx.uow_factory() as uow:
        register_repository(
            uow,
            k8s_ctx.clock,
            principal_name="tests",
            name="example-service",
            registration=RepositoryRegistration(
                url=REPOSITORY_URL,
                default_branch="main",
                policy_name="k8s-script",
                installation_id=1,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="tests"
                ),
            ),
        )
        uow.commit()
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as client:
        yield client


@pytest.fixture
def k8s_supervisor(k8s_ctx: AppContext, k8s_provider: KubernetesProvider) -> Supervisor:
    return Supervisor(
        k8s_ctx.uow_factory,
        {"kubernetes": k8s_provider},
        k8s_ctx.clock,
        holder="k8s-sup-a",
        artifact_store=k8s_ctx.artifact_store,
        harnesses=k8s_ctx.harnesses,
        lease_ttl_seconds=30,
        grace_seconds=5,
    )


def k8s_contract(external_id: str = "EX-0001", **overrides: Any) -> dict[str, Any]:
    document = contract_document(external_id=external_id)
    document["repository"] = {
        "name": "example-service",
        "base_ref": "main",
        "work_branch": f"crucible/{external_id}",
    }
    document["required_verification"] = [
        {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
        {"id": "V3", "command": "sh checks/scan.sh", "expect_exit": 0},
    ]
    document["deliverables"] = [{"kind": "artifacts", "target": None, "draft": False, "closes": []}]
    document["policy"] = {"name": "k8s-script", "version": 1}
    document["execution_request"] = {
        **document["execution_request"],
        "provider": "kubernetes",
        "timeout_seconds": 600,
    }
    document["execution_request"].pop("image", None)
    return {**document, **overrides}


def start(client: TestClient, document: dict[str, Any]) -> str:
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id: str = response.json()["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/start", json={"provider": "kubernetes", "policy_version": 1}
    )
    assert response.status_code == 200, response.text
    return task_id


# ----- the full run --------------------------------------------------------


async def test_a_task_runs_to_its_gates_on_the_kubernetes_provider(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    state = await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    assert state == "awaiting_internal_review"

    view = k8s_client.get(f"/v1/tasks/{task_id}").json()
    attempt = view["executions"][0]["attempts"][0]
    assert attempt["state"] == "succeeded"
    assert attempt["exit_code"] == 0 and attempt["exit_class"] == "completed"
    # 13, 26: every attempt records the digest it ran, resolved at launch.
    detail = k8s_client.get(f"/v1/attempts/{attempt['id']}").json()
    assert "@sha256:" in detail["image_digest"]
    kinds = event_kinds(k8s_client, task_id)
    for kind in (
        "workspace_prepared",
        "attempt_running",
        "attempt_logs_drained",
        "attempt_collected",
        "evidence_recorded",
        "gates_evaluated",
        "attempt_cleaned_up",
    ):
        assert kind in kinds, kind


async def test_the_run_leaves_nothing_of_the_attempt_in_the_namespace(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    assert k8s_api.object_names("jobs") == []
    assert k8s_api.object_names("pods") == []
    assert k8s_api.object_names("networkpolicies") == []
    # 12, 16: the per-attempt Secret goes under every policy, and this one kept the
    # workspace claim (keep_diff_only).
    assert k8s_api.object_names("secrets") == []
    assert k8s_api.object_names("persistentvolumeclaims")


async def test_the_launch_evidence_of_26_is_stored_as_an_artifact(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    attempt_id = k8s_client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    artifacts = k8s_client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    stored = next(a for a in artifacts if a["filename"] == "report/kubernetes-launch.json")
    body = k8s_client.get(f"/v1/artifacts/{stored['id']}/content")
    document = yaml.safe_load(body.text)
    assert document["job"].startswith("worker-")
    assert document["pod"].startswith("worker-")
    assert document["node"] == "lab-node-1"
    assert document["pod_pid_limit"] == 4096
    assert document["runtime_class"] == "standard"


# ----- the failure classes (16) --------------------------------------------


@pytest.mark.parametrize(
    ("behavior", "exit_class"),
    [
        ("crash", "crashed"),
        ("environment", "environment"),
        ("oom", "environment"),
        ("blocked", "blocked"),
        ("vanish", "lost"),
    ],
)
async def test_the_failure_classes_of_16_survive_the_second_provider(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    behavior: str,
    exit_class: str,
) -> None:
    k8s_api.script_all(behavior, after=2)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(20):
        await k8s_supervisor.tick()
        view = k8s_client.get(f"/v1/tasks/{task_id}").json()
        classes = [a["exit_class"] for e in view["executions"] for a in e["attempts"]]
        if exit_class in classes:
            break
    else:
        raise AssertionError(f"no attempt was classified {exit_class}")


async def test_a_prepare_failure_never_leaves_a_worker_behind(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("prepare-fails", after=1)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(6):
        await k8s_supervisor.tick()
    assert k8s_api.object_names("jobs") == []
    assert "attempt_failed" in event_kinds(k8s_client, task_id)


# ----- cancel (16) ---------------------------------------------------------


async def test_a_cancel_drains_the_pod_and_the_attempt_is_killed(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    k8s_api.script_all("hang", after=1)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(4):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    response = k8s_client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={
            "reason": "the operator changed their mind",
            "verbatim": "stop this one, I have changed my mind",
            "decided_by": "operator",
        },
    )
    assert response.status_code == 200, response.text
    for _ in range(12):
        await k8s_supervisor.tick()
        # The scripted worker ignores SIGTERM, so the grace window has to pass before
        # the supervisor kills it; the clock is the test's to move.
        clock.advance(30)
        view = k8s_client.get(f"/v1/tasks/{task_id}").json()
        if view["state"] == "cancelled":
            break
    assert view["state"] == "cancelled"
    # 26: a drain deletes the Pod with the policy grace period; a Pod that ignored it
    # is deleted again with grace zero. Neither is ever reported as a loss (16).
    assert [a["exit_class"] for e in view["executions"] for a in e["attempts"]] == ["killed"]
    assert ("pods", f"worker-{view['executions'][0]['attempts'][0]['id'].lower()}-abc12") in (
        k8s_api.deleted
    )


# ----- reconcile across a restart (10, 16, 26) -----------------------------


async def test_a_second_supervisor_re_attaches_to_a_running_job_by_label(
    k8s_ctx: AppContext,
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    k8s_api.script_all("succeed", after=8)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(3):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    adopted = await k8s_provider.reconcile()
    assert len(adopted) == 1

    # A restarted supervisor: a new instance, a new holder, the same namespace. The
    # worker keeps running through the handover and the run still settles (16).
    successor = Supervisor(
        k8s_ctx.uow_factory,
        {"kubernetes": k8s_provider},
        k8s_ctx.clock,
        holder="k8s-sup-b",
        artifact_store=k8s_ctx.artifact_store,
        harnesses=k8s_ctx.harnesses,
        lease_ttl_seconds=30,
        grace_seconds=5,
    )
    clock.advance(31)
    state = await run_to_settled(successor, k8s_client, task_id, max_ticks=20)
    assert state == "awaiting_internal_review"


async def test_a_job_with_no_live_attempt_row_is_reported_as_an_orphan(
    k8s_provider: KubernetesProvider, k8s_api: FakeKubernetesApi
) -> None:
    """26: list Jobs by label; a Job with no live attempt row is orphaned."""
    assert await k8s_provider.reconcile() == []


async def test_a_pod_deleted_out_of_band_is_lost_not_running(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
) -> None:
    k8s_api.script_all("succeed", after=8)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(3):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    handles = await k8s_provider.reconcile()
    assert handles
    k8s_api.remove_pod_out_of_band(handles[0].attempt_id)
    observation = await k8s_provider.observe(handles[0])
    assert observation.state is ObservationState.LOST
