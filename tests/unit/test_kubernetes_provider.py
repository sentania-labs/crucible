"""The Kubernetes provider against the fake API (08, 26).

No cluster here: the fake answers the API server so the refusals, the state mapping and
the credential paths are exercised deterministically. The real cluster is C8b's
`make e2e-kind` tier.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import (
    KubernetesConfig,
    KubernetesProvider,
    NamespaceProbe,
)
from crucible.ports.execution import (
    CleanupPolicy,
    ExecutionProvider,
    Handle,
    LaunchRefusedError,
    LogOffset,
    ObservationState,
    ProviderError,
)
from crucible.ports.execution import (
    CleanupPolicy as Cleanup,
)
from tests.unit.kubernetes_fixtures import ATTEMPT, IMAGE, build, spec

CODEX_IMAGE = "crucible-worker:codex-fake-succeed-2"


def _auth(last_refresh: str) -> bytes:
    return json.dumps(
        {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": last_refresh}
    ).encode()


async def run_to_exit(provider: KubernetesProvider, handle: Handle) -> Any:
    observation = await provider.observe(handle)
    while observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    return observation


async def prepared(**kwargs: Any) -> Any:
    api, registry, provider = build(**kwargs.pop("build", {}))
    launch = spec(**kwargs)
    api.specs[launch.attempt_id] = launch
    workspace = await provider.prepare(launch)
    return api, registry, provider, launch, workspace


# ----- the port ------------------------------------------------------------


def test_the_provider_satisfies_the_execution_port() -> None:
    _api, _registry, provider = build()
    checked: ExecutionProvider = provider
    assert checked.name == "kubernetes"


def test_capabilities_are_26s() -> None:
    _api, _registry, provider = build()
    capabilities = provider.capabilities().as_dict()
    assert capabilities["isolation"] == "pod"
    assert capabilities["network_control"] is True
    assert capabilities["resource_limits"] is True
    # 26: nothing of a workspace is ever visible to the Crucible process.
    assert capabilities["shared_disk"] is False
    assert capabilities["max_concurrency"] == 3


async def test_max_concurrency_comes_from_the_namespace_resource_quota() -> None:
    api, _registry, provider = build()
    api.create("resourcequotas", {"metadata": {"name": "workers"}, "spec": {"hard": {"pods": "7"}}})
    await provider.health()
    assert provider.capabilities().max_concurrency == 7


# ----- the namespace readiness probe (26) ----------------------------------


async def test_the_probe_passes_when_the_canary_cannot_reach_the_api_server() -> None:
    api, _registry, provider = build()
    api.specs[ATTEMPT] = spec()
    await provider.prepare(spec())
    probe = await provider.ensure_ready()
    assert probe == NamespaceProbe(True, True, 4096, "namespace ready")
    health = await provider.health()
    assert health.state == "ok"
    assert health.checks["egress_enforced"] is True
    assert health.checks["pod_pid_limit"] == 4096
    assert health.checks["runtime_class"] == "standard"


async def test_a_namespace_whose_cni_does_not_enforce_egress_refuses_every_launch() -> None:
    _api, _registry, provider, launch, workspace = await prepared(build={"egress_enforced": False})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.egress_enforced is False
    with pytest.raises(LaunchRefusedError, match="not ready"):
        await provider.launch(workspace, launch)
    assert (await provider.health()).state == "degraded"


async def test_a_node_with_no_pod_pid_limit_refuses_every_launch() -> None:
    _api, _registry, provider, launch, workspace = await prepared(build={"pod_pid_limit": None})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.pid_limit is None
    assert "pod PID limit" in probe.detail
    with pytest.raises(LaunchRefusedError, match="not ready"):
        await provider.launch(workspace, launch)


# ----- images (07, 13) -----------------------------------------------------


async def test_an_image_outside_the_policy_allowlist_is_refused() -> None:
    api, _registry, provider = build()
    launch = spec(policy={"images": {"allowlist": ["ghcr.io/someone-else/worker:*"]}})
    api.specs[launch.attempt_id] = launch
    with pytest.raises(ProviderError, match="outside the policy allowlist"):
        await provider.prepare(launch)


async def test_an_image_whose_harness_label_differs_is_refused() -> None:
    api, _registry, provider = build(harness="codex")
    launch = spec()
    api.specs[launch.attempt_id] = launch
    with pytest.raises(LaunchRefusedError, match="declares harness"):
        await provider.prepare(launch)


async def test_an_image_outside_the_tested_range_is_refused() -> None:
    api, _registry, provider = build(version="9.9.9")
    launch = spec()
    api.specs[launch.attempt_id] = launch
    with pytest.raises(LaunchRefusedError, match="outside the tested range"):
        await provider.prepare(launch)


async def test_the_resolved_digest_is_the_handle_and_the_recorded_image() -> None:
    _api, registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    assert handle.ref == "worker-01attempt0000000000000000a"
    assert handle.image_digest == registry.resolve(IMAGE).reference
    assert "@sha256:" in handle.image_digest


# ----- observe (26) --------------------------------------------------------


async def test_a_running_pod_is_running_and_a_terminated_one_is_its_exit_code() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 0


async def test_an_oom_kill_is_flagged_rather_than_parsed_out_of_the_detail() -> None:
    api, _registry, provider, launch, workspace = await prepared(image=IMAGE)
    api.script(launch.attempt_id, "oom", after=1)
    handle = await provider.launch(workspace, launch)
    observation = await run_to_exit(provider, handle)
    assert observation.exit_code == 137 and observation.oom_killed is True


async def test_a_pod_that_is_gone_with_nothing_crucible_did_is_lost() -> None:
    """26: the Job or Pod no longer exists, so the worker is lost, not exited."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    api.remove_pod_out_of_band(launch.attempt_id)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST


async def test_an_evicted_pod_is_lost() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    api.evict(launch.attempt_id)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST
    assert "Evicted" in (observation.detail or "")


async def test_a_pod_crucible_drained_is_an_exit_and_never_a_loss() -> None:
    """16: a Pod that is gone because Crucible deleted it is a `killed` attempt. The
    only signal Kubernetes offers is a delete, so what Crucible did is remembered."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await provider.terminate(handle, "drain")
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 143
    deleted = [name for kind, name in api.deleted if kind == "pods"]
    assert deleted


async def test_a_worker_that_ignores_sigterm_is_killed_at_the_grace_period() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    api.script(launch.attempt_id, "hang", after=1)
    handle = await provider.launch(workspace, launch)
    await provider.terminate(handle, "drain")
    # The kubelet waited the grace period and the Pod is still there.
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    await provider.terminate(handle, "kill")
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 137


async def test_a_pod_pending_past_the_launch_timeout_is_a_launch_failure() -> None:
    """26: Pending longer than the launch timeout is a launch failure with the Pod's
    conditions as detail, not a stall. Reported as exit 70, which 16 classifies as
    `environment`, so the attempt retries and the tick keeps moving."""
    api, _registry, provider, launch, workspace = await prepared()
    assert (await provider.ensure_ready()).passed
    api.pending_forever.add(launch.attempt_id)
    provider.config = KubernetesConfig(
        poll_interval_seconds=0, launch_timeout_seconds=0, storage_class="lab-ssd"
    )
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70
    assert "Unschedulable" in (observation.detail or "")


async def test_logs_resume_strictly_after_the_stored_position() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    first = await provider.logs(handle, LogOffset())
    assert first and b"fake worker" in first[0].content
    resumed = await provider.logs(
        handle,
        LogOffset(
            index=first[0].lines,
            timestamp=first[0].ts.isoformat() if first[0].ts else None,
            line_sha256=first[0].line_sha256,
            occurrence=first[0].occurrence,
        ),
    )
    assert resumed == []


# ----- reconcile (10, 26) --------------------------------------------------


async def test_reconcile_adopts_a_live_worker_job_by_label() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    adopted = await provider.reconcile()
    assert [(h.attempt_id, h.ref) for h in adopted] == [(launch.attempt_id, handle.ref)]


async def test_reconcile_does_not_adopt_a_job_whose_pod_is_finished() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    assert await provider.reconcile() == []


async def test_retention_removes_what_is_labelled_for_an_attempt_crucible_forgot() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    await provider.launch(workspace, launch)
    assert await provider.retention(keep=[]) > 0
    assert api.object_names("jobs") == []


# ----- credentials (12) ----------------------------------------------------


async def codex_attempt(**build_kwargs: Any) -> Any:
    api, registry, provider = build(harness="codex", **build_kwargs)
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("crucible-harness-codex", {"auth.json": _auth("2026-09-20T00:00:00Z")})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    api.specs[launch.attempt_id] = launch
    workspace = await provider.prepare(launch)
    return api, provider, launch, workspace


async def test_the_per_attempt_secret_is_copied_from_the_harness_secret() -> None:
    api, _provider, _launch, _workspace = await codex_attempt()
    copy = api.harness_secret("cred-01attempt0000000000000000a")
    assert list(copy) == ["auth.json"]
    assert copy["auth.json"] == _auth("2026-09-20T00:00:00Z")


async def test_a_missing_required_auth_file_refuses_the_launch() -> None:
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("crucible-harness-codex", {})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    api.specs[launch.attempt_id] = launch
    with pytest.raises(LaunchRefusedError, match="missing its auth file"):
        await provider.prepare(launch)


async def test_a_harness_with_no_secret_at_all_refuses_the_launch() -> None:
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    launch = spec(harness="codex", image=CODEX_IMAGE)
    api.specs[launch.attempt_id] = launch
    with pytest.raises(LaunchRefusedError, match="not readable"):
        await provider.prepare(launch)


async def test_hermes_needs_no_credential() -> None:
    api, registry, provider = build(harness="hermes")
    image = "crucible-worker:hermes-fake-succeed-1"
    registry.register(image, harness="hermes", version="0.19.0")
    launch = spec(
        harness="hermes",
        image=image,
        endpoint="local",
        endpoint_url="http://10.10.0.42:8000/v1",
        model="gpt-oss:120b",
    )
    api.specs[launch.attempt_id] = launch
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_a_rotated_auth_file_is_written_back_and_the_copy_removed() -> None:
    """12: on a clean exit, a valid, newer file is synced back and the copy removed."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    claim = api.claims["ws-01attempt0000000000000000a"]
    claim["credential/auth.json"] = _auth("2026-09-21T00:00:00Z")
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    sync = outputs.credential_sync
    assert sync is not None and sync.mount_mode == "rw-narrow"
    assert [(f.name, f.synced, f.reason) for f in sync.files] == [
        ("auth.json", True, "changed; newer issued-at, written back")
    ]
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-21T00:00:00Z"
    )
    assert sync.removed and not api.secret_exists("cred-01attempt0000000000000000a")
    assert "credential/auth.json" not in claim


async def test_an_older_auth_file_is_recorded_and_not_written_back() -> None:
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = _auth(
        "2026-09-19T00:00:00Z"
    )
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert outputs.credential_sync.files[0].synced is False
    assert "not newer" in outputs.credential_sync.files[0].reason
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-20T00:00:00Z"
    )


async def test_a_file_that_is_not_the_expected_json_shape_is_not_written_back() -> None:
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = b"not json at all"
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert "not the expected JSON shape" in outputs.credential_sync.files[0].reason
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-20T00:00:00Z"
    )


@pytest.mark.parametrize("policy", list(Cleanup))
async def test_the_per_attempt_secret_is_deleted_under_every_cleanup_policy(
    policy: CleanupPolicy,
) -> None:
    """12, 16: `keep` included. 08's "keep the workspace per policy" never keeps it."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.cleanup(workspace, policy, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_discard_removes_the_secret_of_an_attempt_that_is_never_collected() -> None:
    api, provider, launch, workspace = await codex_attempt()
    await provider.launch(workspace, launch)
    await provider.discard(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_a_launch_that_fails_after_seeding_leaves_no_secret_behind() -> None:
    api, provider, launch, workspace = await codex_attempt()
    api.refuse_create.add("jobs")
    with pytest.raises(ProviderError, match="could not start the worker"):
        await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_the_writable_copy_is_a_claim_leaf_and_the_read_only_form_is_the_secret() -> None:
    """12: a Kubernetes Secret volume is read-only whatever the mount asks for, so
    `rw-narrow` is the claim leaf an init container seeds, which is the same shape and
    the same place the Docker provider puts it."""
    api, provider, launch, workspace = await codex_attempt()
    await provider.launch(workspace, launch)
    pod = next(
        row["body"]["spec"]["template"]["spec"]
        for row in api.created
        if row["kind"] == "jobs" and str(row["name"]).startswith("worker-")
    )
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/home/worker/.codex"] == {
        "name": "ws",
        "mountPath": "/home/worker/.codex",
        "readOnly": False,
        "subPath": "credential",
    }
    # The Crucible-owned template on top of it, read-only, from the identity bundle.
    assert mounts["/home/worker/.codex/config.toml"]["readOnly"] is True
    assert mounts["/home/worker/.codex/config.toml"]["subPath"] == "harness/config.toml"
    init = pod["initContainers"][0]
    assert init["name"] == "credential-seed"
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["cred-source"]["secret"]["secretName"] == "cred-01attempt0000000000000000a"
    assert volumes["cred-source"]["secret"]["items"] == [
        {"key": "auth.json", "path": "auth.json", "mode": 0o400}
    ]


# ----- cleanup (08, 16) ----------------------------------------------------


async def test_delete_removes_the_claim_and_keep_labels_it_for_the_sweep() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.cleanup(workspace, CleanupPolicy.KEEP, launch)
    claim = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    assert claim.body["metadata"]["labels"][k8sspec.LABEL_RETAIN] == "keep"
    assert api.object_names("jobs") == [] and api.object_names("networkpolicies") == []

    api2, _r2, provider2, launch2, workspace2 = await prepared()
    handle2 = await provider2.launch(workspace2, launch2)
    await run_to_exit(provider2, handle2)
    await provider2.cleanup(workspace2, CleanupPolicy.DELETE, launch2)
    assert api2.object_names("persistentvolumeclaims") == []
    assert api2.object_names("configmaps") == []


async def test_keep_diff_only_removes_the_checkout_and_the_tree_and_keeps_the_evidence() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.collect(handle, workspace, launch)
    claim = api.claims["ws-01attempt0000000000000000a"]
    claim["output/tree/README.md"] = b"a clone of the collected state"
    await provider.cleanup(workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)
    assert not [p for p in claim if p.startswith(("repo/", "output/tree/", "credential/"))]
    assert "output/diff.patch" in claim and "output/work_branch.bundle" in claim


# ----- evidence (26) -------------------------------------------------------


async def test_the_attempt_records_26s_observability_fields() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["image_digest"] == handle.image_digest
    assert document["job"] == "worker-01attempt0000000000000000a"
    assert document["pod"] == "worker-01attempt0000000000000000a-abc12"
    assert document["node"] == "lab-node-1"
    assert document["pod_pid_limit"] == 4096
    assert document["runtime_class"] == "standard"
    assert document["network_policy"] == "np-worker-01attempt0000000000000000a"
    assert document["limits"]["cpu"] == "2000m"
    assert document["limits"]["termination_grace_seconds"] == 30
    assert "pypi.org" in document["egress"]
    # Nothing in the record is a value (12).
    assert "auth" not in evidence.content.decode().lower()
