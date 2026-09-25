"""The Kubernetes end-to-end tier against a real kind API, kubelet, CNI and PVC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sapi import (
    KubernetesApiError,
    KubernetesClient,
    kubeconfig_access,
)
from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.harness.registry import default_registry as application_harnesses
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import LoginFlow
from crucible.application.auth import mint_token
from crucible.application.harnesses import HarnessRegistry
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Role
from crucible.ports.execution import (
    CleanupPolicy,
    LaunchRefusedError,
    LaunchSpec,
    LogOffset,
    ObservationState,
)
from crucible.ports.harness import (
    AdapterLaunch,
    AuthFile,
    CredentialSpec,
    HarnessCapabilities,
    LaunchContext,
    MountMode,
)
from tests.e2e.conftest import (
    e2e_contract,
    event_kinds,
    gate_results,
    register,
    run_until,
    submit_and_start,
    upload_review,
)
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.e2e.repo import make_origin
from tests.fixtures import contract_document, promote_for_test

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("CRUCIBLE_E2E_KIND"), reason="needs make e2e-kind"),
]

ATTEMPT_PREFIX = "01KIND00000000000000000"


class RecordingKubernetesClient(KubernetesClient):
    """Keep the last real readiness log after its short-lived Pod is deleted."""

    def pod_log(self, name: str, **kwargs: Any) -> Any:
        frames = super().pod_log(name, **kwargs)
        if name.startswith("crucible-canary-"):
            path = Path(os.environ["CRUCIBLE_E2E_KIND_CANARY_LOG"])
            path.write_bytes(b"".join(frame.payload for frame in frames))
        return frames


class CredentialScriptAdapter(ScriptHarnessAdapter):
    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target="/home/worker/.script-harness",
            auth_files=(AuthFile("auth.json", json=True),),
            minimum_mode=MountMode.RO,
        )


@pytest.fixture(scope="session")
def api() -> KubernetesClient:
    return RecordingKubernetesClient(
        kubeconfig_access(os.environ["CRUCIBLE_E2E_KIND_KUBECONFIG"]),
        "crucible-workers",
        timeout=15,
    )


@pytest.fixture(scope="session")
def registry() -> CraneRegistryClient:
    # The real crane against the tier's own registry on host loopback, which crane
    # reaches over plain HTTP only because it is localhost (e2e-kind.sh puts the pinned
    # binary on PATH).
    return CraneRegistryClient(timeout=15)


@pytest.fixture
def provider(api: KubernetesClient, registry: CraneRegistryClient) -> KubernetesProvider:
    return _provider(api, registry)


def _provider(
    api: KubernetesClient,
    registry: CraneRegistryClient,
    *,
    harnesses: HarnessRegistry | None = None,
) -> KubernetesProvider:
    return KubernetesProvider(
        KubernetesConfig(
            storage_class="standard",
            workspace_size="64Mi",
            cache_claim="crucible-reference-cache",
            poll_interval_seconds=0.25,
            launch_timeout_seconds=45,
            prepare_timeout_seconds=90,
            collector_timeout_seconds=90,
            verifier_timeout_seconds=90,
            cluster_dns_ip=os.environ["CRUCIBLE_E2E_KIND_DNS_IP"],
            broad_egress=True,
            image_repositories=(os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],),
            # kind's containerd gives every container a private cgroup namespace, so the
            # canary cannot see the pod-level cgroup `tools/kind/e2e-kind.sh` configures
            # `podPidsLimit: 512` on (95); this is lab-admin's attestation of that same
            # number for this disposable cluster, the same way a real deployment would.
            pod_pid_limit_override=512,
        ),
        api,
        registry,
        harnesses=harnesses,
    )


def _origin(name: str, behavior: str = "succeed") -> str:
    cache = Path(os.environ["CRUCIBLE_E2E_KIND_CACHE"])
    host_url = make_origin(cache, name, behavior)
    bare = Path(host_url)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "config",
            "remote.origin.url",
            f"file:///crucible/cache/{bare.name}",
        ],
        check=True,
    )
    return f"file:///crucible/cache/{bare.name}"


def _spec(
    number: int,
    repository_url: str,
    *,
    command: tuple[str, ...] = (),
    harness: str = "script-harness",
    network_hosts: tuple[str, ...] = (),
) -> LaunchSpec:
    attempt_id = f"{ATTEMPT_PREFIX}{number:02d}"
    document = contract_document(external_id=f"E2E-KIND-{number:02d}")
    document["repository"] = {
        "name": f"kind-{number}",
        "base_ref": "main",
        "work_branch": f"crucible/E2E-KIND-{number:02d}",
    }
    document["scope"] = {
        "allowed_paths": ["src/**", "checks/**"],
        "prohibited_paths": [".github/**"],
        "may_add_dependencies": False,
        "may_modify_ci": False,
    }
    document["required_verification"] = [
        {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
    ]
    document["execution_request"]["provider"] = "kubernetes"
    return LaunchSpec(
        attempt_id=attempt_id,
        task_id=f"01KINDTASK0000000000000{number:02d}",
        external_id=f"E2E-KIND-{number:02d}",
        role="implement",
        harness=harness,
        model="none",
        image=os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
        timeout_seconds=60,
        contract=document,
        command=command,
        network="policy",
        endpoint="subscription",
        policy={
            "images": {"allowlist": ["localhost:*/*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": list(network_hosts)},
            "resources": {"cpus": 0.25, "memory": "256MiB", "ephemeral_storage": "256Mi"},
            "limits": {"grace_seconds": 2},
        },
        repository_url=repository_url,
    )


async def _terminal(provider: KubernetesProvider, handle: Any, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = await provider.observe(handle)
        if observed.state is not ObservationState.RUNNING:
            return observed
        await asyncio.sleep(0.25)
    raise AssertionError("the real worker Pod never became terminal")


async def _running(provider: KubernetesProvider, handle: Any, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = await provider._pod_of(handle.ref)
        if pod and str((pod.get("status") or {}).get("phase")) == "Running":
            return
        await asyncio.sleep(0.25)
    raise AssertionError("the real worker Pod never ran")


async def _pods_gone(api: KubernetesClient, attempt_id: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    selector = f"{k8sspec.LABEL_ATTEMPT}={attempt_id}"
    while time.monotonic() < deadline:
        if not api.list_objects("pods", label_selector=selector):
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"pods for {attempt_id} survived deletion")


def _network_destinations() -> dict[str, str]:
    return {
        "api-server": f"https://{os.environ['CRUCIBLE_E2E_KIND_API_IP']}:443/version",
        "cluster-dns-wrong-port": f"http://{os.environ['CRUCIBLE_E2E_KIND_DNS_IP']}:443/",
        "another-namespace": f"http://{os.environ['CRUCIBLE_E2E_KIND_PEER_IP']}:443/",
        "link-local": "http://169.254.169.254:443/",
        "lab-10": "http://10.0.0.1:443/",
        "lab-172": "http://172.16.0.1:443/",
        "lab-192": "http://192.168.0.1:443/",
        "lab-carrier": "http://100.64.0.1:443/",
    }


def _network_script(destinations: dict[str, str], *, attempts: int) -> str:
    probes = "\n".join(
        f"( reached=0; for i in $(seq 1 {attempts}); do "
        f"if curl -k -sS -o /dev/null --connect-timeout 1 --max-time 1 '{url}'; "
        f"then reached=1; break; fi; sleep 0.25; done; "
        f"if [ $reached -eq 1 ]; then echo '{name}=reached'; else echo '{name}=denied'; fi ) &"
        for name, url in destinations.items()
    )
    return f"{probes}\nwait"


async def _unrestricted_network_control(api: KubernetesClient, destinations: dict[str, str]) -> str:
    name = "network-reachability-control"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "crucible-workers"},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "serviceAccountName": "crucible-worker",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "control",
                    "image": os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
                    "command": ["sh", "-c", _network_script(destinations, attempts=20)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "250m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    with contextlib.suppress(KubernetesApiError):
        api.delete("pods", name, grace_period_seconds=0)
    api.create("pods", pod)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        phase = str((api.get("pods", name).get("status") or {}).get("phase", ""))
        if phase in ("Succeeded", "Failed"):
            frames = api.pod_log(name, container="control", timestamps=False)
            api.delete("pods", name, grace_period_seconds=0)
            assert phase == "Succeeded"
            return b"".join(frame.payload for frame in frames).decode("utf-8", "replace")
        await asyncio.sleep(0.25)
    raise AssertionError("the unrestricted network control Pod did not finish")


async def test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness rows 5, 7 and 11: logs, failure detection and safe termination."""
    spec = _spec(1, _origin("full-lifecycle"))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    assert handle.image_digest and "@sha256:" in handle.image_digest
    observed = await _terminal(provider, handle)
    assert observed.state is ObservationState.EXITED and observed.exit_code == 0
    chunks = await provider.logs(handle, LogOffset())
    assert b"read identity bundle" in b"".join(chunk.content for chunk in chunks)
    outputs = await provider.collect(handle, workspace, spec)
    assert outputs.report is not None
    assert {run.id for run in outputs.verifications if run.ran} == {"V1", "V2"}
    assert outputs.bundle is not None
    await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)
    await _pods_gone(api, spec.attempt_id)


async def test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """The shared app and Supervisor lifecycle, backed by a real Job and PVC."""
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-store"),
        harnesses=harnesses,
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"kind-{role.value}", role=role).token
        uow.commit()

    app = create_app(ctx)
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (
            200,
            201,
        )
        policy = e2e_policy_document()
        policy["description"] = "The e2e script harness on the Kubernetes provider."
        policy["images"]["allowlist"] = ["localhost:*/*"]
        policy["resources"] = {
            "cpus": 1,
            "memory": "256MiB",
            "pids": 128,
            "tmpfs_total": "256MiB",
        }
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201)
        with ctx.uow_factory() as uow:
            promote_for_test(
                uow,
                digest=resolved.digest,
                reference=resolved.reference,
                harnesses=dict(resolved.harnesses) or {"script-harness": "1.0.0"},
                at=clock.now(),
                by="e2e-kind",
                reason="kind script harness image",
            )
            uow.commit()

    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        origin = _origin("supervisor-lifecycle")
        register(ctx, "supervisor-lifecycle", origin)
        document = e2e_contract("E2E-KIND-SUPERVISOR", "supervisor-lifecycle", image)
        document["execution_request"]["provider"] = "kubernetes"
        task_id = submit_and_start(client, document)
        first = Supervisor(
            ctx.uow_factory,
            {"kubernetes": provider},
            clock,
            holder="e2e-kind-first",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        for _ in range(60):
            await first.tick()
            attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
            if attempt and attempt["state"] == "running":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("the Kubernetes-backed Supervisor never launched a worker")
        await first.stop()

        successor_provider = _provider(api, registry)
        successor = Supervisor(
            ctx.uow_factory,
            {"kubernetes": successor_provider},
            clock,
            holder="e2e-kind-successor",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        state = await run_until(
            successor,
            client,
            task_id,
            {"awaiting_internal_review", "pre_pr_gates_failed"},
            max_ticks=90,
            pause=0.5,
        )
        results = gate_results(client, task_id)
        assert state == "awaiting_internal_review", json.dumps(results, sort_keys=True)
        for gate in (
            "verification_ran",
            "workspace_clean",
            "commits_present",
            "no_injected_files",
            "no_secrets",
        ):
            assert results[gate] == "pass", results
        upload_review(client, task_id)
        assert (
            await run_until(successor, client, task_id, {"awaiting_acceptance"})
            == "awaiting_acceptance"
        )

        attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT image_digest, identity_sha256, logs_drained_at, cleaned_up_at "
                    "FROM attempts WHERE id = :id"
                ),
                {"id": attempt_id},
            ).one()
            chunks = connection.execute(
                text("SELECT content, gzipped FROM log_chunks WHERE attempt_id = :id ORDER BY id"),
                {"id": attempt_id},
            ).all()
        assert row.image_digest and "@sha256:" in row.image_digest
        assert row.identity_sha256 and len(row.identity_sha256) == 64
        assert row.logs_drained_at is not None
        assert row.cleaned_up_at is not None and row.cleaned_up_at >= row.logs_drained_at
        body = b"".join(chunk.content for chunk in chunks if not chunk.gzipped).decode(
            "utf-8", "replace"
        )
        assert body.count("read identity bundle") == 1
        for kind in (
            "workspace_prepared",
            "image_resolved",
            "attempt_logs_drained",
            "verification_completed",
            "attempt_cleaned_up",
        ):
            assert kind in event_kinds(client, task_id)

        timeout_origin = _origin("supervisor-timeout", "hang")
        register(ctx, "supervisor-timeout", timeout_origin)
        timeout_document = e2e_contract("E2E-KIND-TIMEOUT", "supervisor-timeout", image)
        timeout_document["execution_request"].update(
            {"provider": "kubernetes", "timeout_seconds": 5}
        )
        timeout_task = submit_and_start(client, timeout_document)
        assert await run_until(
            successor,
            client,
            timeout_task,
            {"awaiting_internal_review", "pre_pr_gates_failed"},
            max_ticks=60,
            pause=0.5,
        ) in {"awaiting_internal_review", "pre_pr_gates_failed"}
        timeout_attempt = client.get(f"/v1/tasks/{timeout_task}").json()["latest_attempt"]
        with engine.begin() as connection:
            timeout_row = connection.execute(
                text("SELECT exit_class, termination_reason FROM attempts WHERE id = :id"),
                {"id": timeout_attempt["id"]},
            ).one()
        assert timeout_row.exit_class == "timeout"
        assert timeout_row.termination_reason == "timeout"
        assert "attempt_timeout_drain" in event_kinds(client, timeout_task)

        cancel_origin = _origin("supervisor-cancel", "hang")
        register(ctx, "supervisor-cancel", cancel_origin)
        cancel_document = e2e_contract("E2E-KIND-CANCEL", "supervisor-cancel", image)
        cancel_document["execution_request"]["provider"] = "kubernetes"
        cancel_task = submit_and_start(client, cancel_document)
        for _ in range(60):
            await successor.tick()
            cancel_attempt = client.get(f"/v1/tasks/{cancel_task}").json().get("latest_attempt")
            if cancel_attempt and cancel_attempt["state"] == "running":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("the cancellation worker never reached running")
        cancelled = client.post(
            f"/v1/tasks/{cancel_task}/cancel",
            json={
                "reason": "kind cancellation case",
                "verbatim": "cancel the kind test worker",
                "decided_by": "tests",
            },
        )
        assert cancelled.status_code == 200
        assert (
            await run_until(successor, client, cancel_task, {"cancelled"}, max_ticks=40, pause=0.5)
            == "cancelled"
        )
        assert client.get(f"/v1/tasks/{cancel_task}").json()["latest_attempt"]["exit_class"] in (
            "killed",
            "cancelled",
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                    "'{limits,stall_warn_seconds}', '2'), '{limits,stall_fail_seconds}', '4') "
                    "WHERE name='e2e-script' AND version=1"
                )
            )
        stall_origin = _origin("supervisor-stall", "hang")
        register(ctx, "supervisor-stall", stall_origin)
        stall_document = e2e_contract("E2E-KIND-STALL", "supervisor-stall", image)
        stall_document["execution_request"]["provider"] = "kubernetes"
        stall_task = submit_and_start(client, stall_document)
        await run_until(
            successor,
            client,
            stall_task,
            {"awaiting_internal_review", "pre_pr_gates_failed"},
            max_ticks=50,
            pause=0.5,
        )
        stall_attempt = client.get(f"/v1/tasks/{stall_task}").json()["latest_attempt"]
        with engine.begin() as connection:
            stall_row = connection.execute(
                text("SELECT exit_class, termination_reason FROM attempts WHERE id = :id"),
                {"id": stall_attempt["id"]},
            ).one()
        assert stall_row.exit_class == "timeout"
        assert stall_row.termination_reason == "stall"
        assert {"worker_quiet", "worker_stalled"} <= set(event_kinds(client, stall_task))

        detached_origin = _origin("supervisor-detached")
        register(ctx, "supervisor-detached", detached_origin)
        detached_document = e2e_contract("E2E-KIND-DETACHED", "supervisor-detached", image)
        detached_document["execution_request"]["provider"] = "kubernetes"
        detached_document["required_verification"].extend(
            [
                {"id": "check/one", "command": "sh checks/lint.sh", "expect_exit": 0},
                {"id": "check_one", "command": "exit 3", "expect_exit": 3},
            ]
        )
        detached_task = submit_and_start(client, detached_document)
        for _ in range(90):
            await successor.tick()
            with ctx.uow_factory() as uow:
                detached = uow.tasks.get(detached_task)
                assert detached is not None
                if detached.state.value in ("awaiting_internal_review", "pre_pr_gates_failed"):
                    break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("the detached Kubernetes task did not finish")
        detached_attempt = client.get(f"/v1/tasks/{detached_task}").json()["latest_attempt"]
        evidence = client.get(f"/v1/attempts/{detached_attempt['id']}/evidence").json()["items"]
        verification_ids = {
            item["payload"]["id"] for item in evidence if item["kind"] == "verification_run"
        }
        assert {"check/one", "check_one"} <= verification_ids
        with ctx.uow_factory() as uow:
            detached_task_row = uow.tasks.get(detached_task)
            assert detached_task_row is not None
            assert uow.wakes.list_for_principal(
                detached_task_row.principal_id, since=None, include_acked=False, limit=50
            )

        orphan = _spec(40, _origin("supervisor-orphan"), command=("sh", "-c", "sleep 600"))
        orphan_workspace = await successor_provider.prepare(orphan)
        orphan_handle = await successor_provider.launch(orphan_workspace, orphan)
        await _running(successor_provider, orphan_handle)
        assert (await successor.tick()).orphans >= 1
        await _pods_gone(api, orphan.attempt_id)
        await successor_provider.cleanup(orphan_workspace, CleanupPolicy.DELETE, orphan)


async def test_row_12_concurrent_attempts_use_distinct_claims(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness row 12: two live attempts never share a working tree."""
    origin = _origin("concurrent-claims")
    first = _spec(30, origin, command=("sh", "-c", "sleep 3"))
    second = _spec(31, origin, command=("sh", "-c", "sleep 3"))
    first_ws, second_ws = await asyncio.gather(provider.prepare(first), provider.prepare(second))
    first_handle, second_handle = await asyncio.gather(
        provider.launch(first_ws, first), provider.launch(second_ws, second)
    )
    await asyncio.gather(_terminal(provider, first_handle), _terminal(provider, second_handle))
    attempts = {first.attempt_id, second.attempt_id}
    claims = {
        str((row.get("metadata") or {}).get("name"))
        for row in api.list_objects("persistentvolumeclaims")
        if str((row.get("metadata") or {}).get("labels", {}).get(k8sspec.LABEL_ATTEMPT)) in attempts
    }
    assert claims == {
        k8sspec.object_name("ws", first.attempt_id),
        k8sspec.object_name("ws", second.attempt_id),
    }
    await asyncio.gather(
        provider.cleanup(first_ws, CleanupPolicy.DELETE, first),
        provider.cleanup(second_ws, CleanupPolicy.DELETE, second),
    )


async def test_network_policy_denies_every_kubernetes_destination_from_the_worker(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness row 12: the real CNI denies every destination listed by spec 26."""
    destinations = _network_destinations()
    default_deny = api.get("networkpolicies", "default-deny")
    api.delete("networkpolicies", "default-deny")
    try:
        control = await _unrestricted_network_control(api, destinations)
        for name in destinations:
            assert f"{name}=reached" in control, control
    finally:
        restored = {key: value for key, value in default_deny.items() if key != "status"}
        metadata = restored["metadata"]
        restored["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key in ("name", "namespace", "labels", "annotations")
        }
        with contextlib.suppress(KubernetesApiError):
            api.create("networkpolicies", restored)
    await asyncio.sleep(2)
    spec = _spec(
        2,
        _origin("network-denials"),
        command=("sh", "-c", _network_script(destinations, attempts=20)),
        network_hosts=("example.com",),
    )
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    assert (await _terminal(provider, handle)).exit_code == 0
    body = b"".join(chunk.content for chunk in await provider.logs(handle, LogOffset())).decode()
    for name in destinations:
        assert f"{name}=denied" in body, body
        assert f"{name}=reached" not in body, body
    await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)


async def test_deleted_pod_is_lost_and_sigterm_ignoring_pod_dies_at_grace(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Rows 7 and 11: out-of-band loss, then kubelet SIGKILL after the grace."""
    lost_spec = _spec(3, _origin("lost"), command=("sh", "-c", "sleep 600"))
    lost_ws = await provider.prepare(lost_spec)
    lost_handle = await provider.launch(lost_ws, lost_spec)
    await _running(provider, lost_handle)
    # 103: lost requires having seen the Pod at least once, to tell it apart from a
    # Job whose Pod the controller has not created yet.
    assert (await provider.observe(lost_handle)).state is ObservationState.RUNNING
    pod = await provider._pod_of(lost_handle.ref)
    assert pod is not None
    api.delete("pods", str(pod["metadata"]["name"]), grace_period_seconds=0)
    await _pods_gone(api, lost_spec.attempt_id)
    assert (await provider.observe(lost_handle)).state is ObservationState.LOST
    await provider.cleanup(lost_ws, CleanupPolicy.DELETE, lost_spec)

    stubborn = _spec(
        4,
        _origin("stubborn"),
        command=("sh", "-c", "trap '' TERM; echo ready; while :; do sleep 1; done"),
    )
    stubborn_ws = await provider.prepare(stubborn)
    stubborn_handle = await provider.launch(stubborn_ws, stubborn)
    await _running(provider, stubborn_handle)
    started = time.monotonic()
    await provider.terminate(stubborn_handle, "drain")
    assert (await _terminal(provider, stubborn_handle, timeout=15)).state is ObservationState.EXITED
    assert time.monotonic() - started >= 1.5
    await provider.cleanup(stubborn_ws, CleanupPolicy.DELETE, stubborn)


async def test_restart_adopts_the_job_and_resumes_logs(
    provider: KubernetesProvider, api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Readiness row 5: a fresh provider adopts the Job and resumes after its offset."""
    script = "i=1; while [ $i -le 8 ]; do echo resume-$i; i=$((i+1)); sleep 1; done"
    spec = _spec(5, _origin("restart"), command=("sh", "-c", script))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _running(provider, handle)
    first = await provider.logs(handle, LogOffset())
    assert first
    last = first[-1]
    offset = LogOffset(
        index=sum(chunk.lines for chunk in first),
        timestamp=last.ts.isoformat() if last.ts else None,
        line_sha256=last.line_sha256,
        occurrence=last.occurrence,
    )
    successor = _provider(api, registry)
    adopted = await successor.reconcile()
    adopted_handle = next(item for item in adopted if item.attempt_id == spec.attempt_id)
    await asyncio.sleep(2)
    resumed = await successor.logs(adopted_handle, offset)
    assert resumed
    assert not set(b"".join(c.content for c in first).splitlines()) & set(
        b"".join(c.content for c in resumed).splitlines()
    )
    await _terminal(successor, adopted_handle)
    await successor.cleanup(workspace, CleanupPolicy.DELETE, spec)


@pytest.mark.parametrize("policy", list(CleanupPolicy))
async def test_per_attempt_secret_is_removed_under_every_cleanup_policy(
    api: KubernetesClient,
    registry: CraneRegistryClient,
    policy: CleanupPolicy,
) -> None:
    harnesses = HarnessRegistry((CredentialScriptAdapter(),))
    provider = _provider(api, registry, harnesses=harnesses)
    source = "crucible-harness-script-harness"
    try:
        api.create(
            "secrets",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": source, "namespace": "crucible-workers"},
                "type": "Opaque",
                "stringData": {"auth.json": json.dumps({"placeholder": "x" * 32})},
            },
        )
    except KubernetesApiError as exc:
        if exc.status != 409:
            raise
    number = 10 + list(CleanupPolicy).index(policy)
    spec = _spec(number, _origin(f"secret-{policy.value}"))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _terminal(provider, handle)
    await provider.cleanup(workspace, policy, spec)
    with pytest.raises(KubernetesApiError) as raised:
        api.get("secrets", k8sspec.object_name("cred", spec.attempt_id))
    assert raised.value.status == 404


async def test_probe_refuses_launches_without_default_deny(
    api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Readiness row 12: removing enforcement makes the canary fail closed."""
    spec = _spec(20, _origin("probe-refusal"))
    provider = _provider(api, registry)
    workspace = await provider.prepare(spec)
    default_deny = api.get("networkpolicies", "default-deny")
    api.delete("networkpolicies", "default-deny")
    try:
        probe = await provider.ensure_ready()
        assert probe.passed is False
        assert probe.egress_enforced is False
        assert "reached the API server" in probe.detail
        with pytest.raises(LaunchRefusedError, match="namespace is not ready"):
            await provider.launch(workspace, spec)
    finally:
        restored = {key: value for key, value in default_deny.items() if key not in ("status",)}
        metadata = restored["metadata"]
        restored["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key in ("name", "namespace", "labels", "annotations")
        }
        api.create("networkpolicies", restored)
        await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)


# ----- the login Job, the service-owned Secret and the probe (25, 26, ADR 0015) ----------

# The stand-in harness's credential directory and its "model endpoint". The endpoint is
# a public name the worker's policy permits (the tier's provider renders the broad rule),
# so the attempt reaching it and the login Job not reaching it is the policy's doing.
STAND_IN_DIR = "/home/worker/.crucible-login"
STAND_IN_MODEL_ENDPOINT = "example.com"


def _reach(host: str) -> str:
    return (
        "r=refused; for i in 1 2 3; do "
        f"if curl -sS -o /dev/null --connect-timeout 5 --max-time 10 https://{host}/; "
        "then r=reached; break; fi; sleep 1; done; "
        'echo "model-endpoint=$r"; '
    )


class StandInLoginAdapter(ScriptHarnessAdapter):
    """A subscription harness stood in for by the script-harness image, the way that
    image stands in for the real harnesses: its login (`crucible-script-harness
    login-stub`) prints a device URL and code and writes `session.json`, its credential
    is that one file, rw-narrow like the real three, and it declares a model endpoint
    and no login endpoint. Its launch refuses to run without the credential."""

    def capabilities(self) -> HarnessCapabilities:
        return replace(
            super().capabilities(), endpoints=(STAND_IN_MODEL_ENDPOINT,), login_endpoints=()
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target=STAND_IN_DIR,
            auth_files=(AuthFile("session.json", json=True, json_keys=("authenticated",)),),
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env="CRUCIBLE_LOGIN_DIR",
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        guard = (
            f'test -s "{STAND_IN_DIR}/session.json" '
            '|| { echo "no stand-in credential" >&2; exit 70; }; '
            "echo credential-present; " + _reach(STAND_IN_MODEL_ENDPOINT)
        )
        if ctx.attempt_id == "probe":
            return AdapterLaunch(argv=("sh", "-c", guard + "exit 0"), workdir=ctx.repo_mount)
        return AdapterLaunch(
            argv=("sh", "-c", guard + "exec crucible-script-harness"), workdir=ctx.repo_mount
        )


STAND_IN_FLOW = LoginFlow(
    harness="script-harness",
    argv=("crucible-script-harness", "login-stub"),
    image_binary="/usr/local/bin/crucible-script-harness",
    directory_env="CRUCIBLE_LOGIN_DIR",
    directory_subdir="",
    pastes_code=False,
    captures_token=False,
    token_pattern="",
    token_file="",
    window="stand-in device flow",
)


def _poll_login(admin: TestClient, *states: str, timeout: float = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = admin.get("/v1/admin/credentials/script-harness/login").json()
        if state["state"] in states:
            return state
        time.sleep(0.5)
    raise AssertionError(f"the stand-in login never reached {states}: {state}")


async def test_login_from_an_empty_secret_to_a_probe_and_an_attempt_through_the_admin_api(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """#92 and #58 on a real cluster: the admin API runs the stand-in harness's login as
    a Job, stores what it wrote in the Secret the service owns, probes it, and one routed
    attempt runs with it. The login Job cannot reach the model endpoint the attempt can."""
    harnesses = HarnessRegistry((StandInLoginAdapter(),))
    provider = _provider(api, registry, harnesses=harnesses)
    clock = SystemClock()
    secret = provider.credential_secret("script-harness")
    with contextlib.suppress(KubernetesApiError):
        api.delete("secrets", secret)
    # What a GitOps-era deployment leaves behind: the Secret exists and holds nothing.
    api.create(
        "secrets",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret, "namespace": "crucible-workers"},
            "type": "Opaque",
        },
    )
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-login-store"),
        harnesses=harnesses,
    )
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=clock,
        providers={"fake": FakeProvider(), "kubernetes": provider},
        harnesses=harnesses,
        # The supervisor below ticks once up front; its lease outlives the login.
        lease_ttl_seconds=300,
        login_timeout_seconds=180,
        probe_timeout_seconds=120,
        login_flows={"script-harness": STAND_IN_FLOW},
        # The stand-in's CLI checks the model endpoint before it logs in, so the
        # login's own output says whether its Job could reach it.
        login_commands={
            "script-harness": (
                "sh",
                "-c",
                _reach(STAND_IN_MODEL_ENDPOINT)
                + "exec /usr/local/bin/crucible-script-harness login-stub",
            )
        },
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"login-{role.value}", role=role).token
        uow.commit()
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-login",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        grace_seconds=5,
        harnesses=harnesses,
    )
    await supervisor.tick()
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    app = create_app(ctx)
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
            routing = e2e_routing_document()
            assert admin.put(
                f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
            ).status_code in (200, 201)
            policy = e2e_policy_document()
            policy["images"]["allowlist"] = ["localhost:*/*"]
            policy["resources"] = {
                "cpus": 1,
                "memory": "256MiB",
                "pids": 128,
                "tmpfs_total": "256MiB",
            }
            uploaded = admin.put(f"/v1/policies/{policy['name']}/{policy['version']}", json=policy)
            assert uploaded.status_code in (200, 201), uploaded.text
            with ctx.uow_factory() as uow:
                promote_for_test(
                    uow,
                    digest=resolved.digest,
                    reference=resolved.reference,
                    harnesses=dict(resolved.harnesses),
                    at=clock.now(),
                    by="e2e-kind",
                    reason="the stand-in login's image",
                )
                uow.commit()

            before = admin.get("/v1/admin/credentials/script-harness").json()
            assert before["state"] == "absent", before
            assert before["source"]["exists"] is True
            assert before["source"]["service_owned"] is False

            started = admin.post(
                "/v1/admin/credentials/script-harness/login", json={"reason": "kind login"}
            )
            assert started.status_code == 200, started.text
            state = _poll_login(admin, "finished", "failed")
            print("login:", json.dumps(state, indent=2))
            assert state["state"] == "finished", state
            assert state["url"] == "https://example.invalid/device"
            assert state["code"] == "C7AA-TEST"
            assert state["credential_written"] is True
            # The login lock (25, 26) was taken on the real API server and is released
            # by uid once the Job is gone.
            lock_selector = f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN_LOCK}"
            for _ in range(60):
                if not api.list_objects("configmaps", label_selector=lock_selector):
                    break
                time.sleep(0.5)
            assert not api.list_objects("configmaps", label_selector=lock_selector)
            # 58: the login Job's egress is its login endpoints, and this harness has
            # none, so the model endpoint is refused from it.
            assert "model-endpoint=refused" in state["output_tail"], state
            finished = admin.post(
                "/v1/admin/credentials/script-harness/login/finish",
                json={"reason": "kind login done"},
            )
            assert finished.json()["shape"]["ok"] is True, finished.text
            stored = api.get("secrets", secret)
            print("secret labels:", stored["metadata"].get("labels"))
            assert stored["metadata"]["labels"][k8sspec.LABEL_MANAGED_BY] == "crucible"
            assert set(stored["data"]) == {"session.json"}
            assert not api.list_objects(
                "jobs", label_selector=f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN}"
            )
            assert not api.list_objects(
                "networkpolicies", label_selector=f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN}"
            )

            validated = admin.post(
                "/v1/admin/credentials/script-harness/validate", json={"reason": "kind probe"}
            )
            assert validated.status_code == 200, validated.text
            report = validated.json()
            print("validate:", json.dumps(report, indent=2))
            assert report["probe"]["exit_class"] == "completed", report
            assert report["validated"] is True
            assert report["credential"]["state"] == "validated"
            assert not api.list_objects(
                "persistentvolumeclaims", label_selector=f"{k8sspec.LABEL_ADMIN}=probe"
            )

        with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
            register(ctx, "kind-login", _origin("kind-login"))
            document = e2e_contract("E2E-KIND-LOGIN", "kind-login", image)
            document["execution_request"]["provider"] = "kubernetes"
            task_id = submit_and_start(client, document)
            result = await run_until(
                supervisor,
                client,
                task_id,
                {"awaiting_internal_review", "pre_pr_gates_failed"},
                max_ticks=120,
                pause=0.5,
            )
            attempt = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]
            print("attempt:", task_id, attempt["state"], attempt.get("exit_class"), result)
            with engine.begin() as connection:
                chunks = connection.execute(
                    text("SELECT content, gzipped FROM log_chunks WHERE attempt_id = :id"),
                    {"id": attempt["id"]},
                ).all()
            body = b"".join(c.content for c in chunks if not c.gzipped).decode("utf-8", "replace")
            print("attempt log:", body[-1500:])
            assert "credential-present" in body
            assert "model-endpoint=reached" in body, body
            assert attempt["exit_class"] == "completed", attempt
            events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
            synced = [e for e in events["items"] if e["kind"] == "credential_synced"]
            print("credential sync:", json.dumps([e["payload"] for e in synced], indent=2))
    finally:
        with contextlib.suppress(KubernetesApiError):
            api.delete("secrets", secret)
