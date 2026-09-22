"""26's pod shape, field by field, for every role (requirement 2 of C8a).

The point of reading each field separately is that a regression in any one of them
fails on its own line. A Pod Security `restricted` namespace refuses the same fields
from outside Crucible, so these assertions and that admission are two enforcements of
one shape; this tier is the one that runs without a cluster.
"""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
    CleanupPolicy,
)
from tests.unit.kubernetes_fixtures import build, pod_of, spec

# Every object an attempt's roles produce, by the prefix its name carries.
ROLE_PREFIXES = (
    "preparer-",
    "worker-",
    "collector-",
    "bundle-verifier-",
    "verifier-",
    "reader-",
    "cleaner-",
)


@pytest.fixture
async def ran() -> Any:
    """One whole attempt against the fake API, so every role's Pod has been rendered."""
    api, _registry, provider = build()
    launch = spec()
    api.specs[launch.attempt_id] = launch
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    await provider.collect(handle, workspace, launch)
    await provider.cleanup(workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)
    return api, provider, launch


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_pod_security_context_is_26s_verbatim(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_container_security_context_is_26s_verbatim(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    container = pod_of(api, prefix)["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_no_service_account_token_no_host_namespaces_no_service_links(
    ran: Any, prefix: str
) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "crucible-worker"
    assert pod["enableServiceLinks"] is False
    assert pod["hostNetwork"] is False
    assert pod["hostPID"] is False
    assert pod["hostIPC"] is False
    assert pod["restartPolicy"] == "Never"


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_runtime_class_is_not_set(ran: Any, prefix: str) -> None:
    """26: a runtime class is not set in this version; the field is the microVM step."""
    api, _provider, _launch = ran
    assert "runtimeClassName" not in pod_of(api, prefix)


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_limits_come_from_policy_and_the_grace_period_with_them(
    ran: Any, prefix: str
) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    resources = pod["containers"][0]["resources"]
    assert resources["limits"]["cpu"] == "2000m"
    assert resources["limits"]["memory"] == str(4 * 1024**3)
    assert resources["limits"]["ephemeral-storage"] == "2Gi"
    # Requests equal limits: a worker promised the policy's memory is not the first
    # thing evicted, which would show up as a `lost` attempt nobody caused (16).
    assert resources["requests"] == {"cpu": "2000m", "memory": str(4 * 1024**3)}
    assert pod["terminationGracePeriodSeconds"] == 30


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_tmp_and_home_are_memory_backed_and_size_limited(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    volumes = {v["name"]: v for v in pod["volumes"]}
    for name in ("tmp", "home"):
        assert volumes[name]["emptyDir"]["medium"] == "Memory"
        assert volumes[name]["emptyDir"]["sizeLimit"] == str(512 * 1024**2)
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/tmp"]["name"] == "tmp"
    assert mounts["/home/worker"]["name"] == "home"


async def test_the_worker_mount_layout(ran: Any) -> None:
    """26's mount layout, at the paths the identity bundle names (06): the bundle tells
    the worker its checkout is at REPO_MOUNT and its report directory at REPORT_MOUNT."""
    api, _provider, _launch = ran
    pod = pod_of(api, "worker-")
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts[REPO_MOUNT] == {
        "name": "ws",
        "mountPath": REPO_MOUNT,
        "readOnly": False,
        "subPath": "repo",
    }
    assert mounts[REPORT_MOUNT]["subPath"] == "report"
    assert mounts[REPORT_MOUNT]["readOnly"] is False
    assert mounts[IDENTITY_MOUNT]["readOnly"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["ws"]["persistentVolumeClaim"]["claimName"] == "ws-01attempt0000000000000000a"
    assert volumes["identity"]["configMap"]["name"] == "identity-01attempt0000000000000000a"
    # Every bundle file is projected at its own relative path, not at a flattened key.
    paths = {item["path"] for item in volumes["identity"]["configMap"]["items"]}
    assert "IDENTITY.md" in paths and "contract.yaml" in paths


async def test_the_preparer_gets_the_whole_claim_and_the_collector_gets_it_read_only(
    ran: Any,
) -> None:
    api, _provider, _launch = ran

    def mounts(prefix: str) -> dict[str, Any]:
        return {m["mountPath"]: m for m in pod_of(api, prefix)["containers"][0]["volumeMounts"]}

    preparer = mounts("preparer-")
    assert preparer[WORK_MOUNT]["readOnly"] is False and "subPath" not in preparer[WORK_MOUNT]

    collector = mounts("collector-")
    # 08: the checkout and the report directory read-only, an output directory writable.
    assert collector[REPO_MOUNT]["readOnly"] is True
    assert collector[REPORT_MOUNT]["readOnly"] is True
    assert collector[OUTPUT_MOUNT]["readOnly"] is False

    bundle = mounts("bundle-")
    assert bundle[OUTPUT_MOUNT]["readOnly"] is True

    verifier = mounts("verifier-")
    # 11: the verifier sees its own tree and its own log directory, never the
    # collector's output directory.
    assert verifier[REPO_MOUNT]["subPath"] == "output/tree"
    assert verifier[VERIFY_MOUNT]["subPath"] == "verify"
    assert OUTPUT_MOUNT not in verifier

    reader = mounts("reader-")
    assert reader[WORK_MOUNT]["readOnly"] is True


async def test_the_job_never_retries_and_carries_its_own_deadline(ran: Any) -> None:
    api, _provider, _launch = ran
    job = next(row["body"] for row in api.created if str(row["name"]).startswith("worker-"))
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    # The contract's timeout plus the drain grace: Crucible drains before the deadline
    # and classifies the exit itself (26).
    assert job["spec"]["activeDeadlineSeconds"] == 630


async def test_every_object_carries_26s_four_labels_and_lives_in_the_workers_namespace(
    ran: Any,
) -> None:
    api, _provider, launch = ran
    attempt_objects = [
        row
        for row in api.created
        if (row["body"].get("metadata") or {}).get("labels", {}).get(k8sspec.LABEL_ATTEMPT)
    ]
    assert attempt_objects
    for row in attempt_objects:
        labels = row["body"]["metadata"]["labels"]
        assert labels[k8sspec.LABEL_ATTEMPT] == launch.attempt_id
        assert labels[k8sspec.LABEL_TASK] == launch.task_id
        assert labels[k8sspec.LABEL_OWNER] == launch.owner
        assert labels[k8sspec.LABEL_ROLE]
        assert row["body"]["metadata"]["namespace"] == "crucible-workers"


async def test_the_image_pull_secret_is_on_every_pod_when_one_is_configured(ran: Any) -> None:
    api, _provider, _launch = ran
    for prefix in ROLE_PREFIXES:
        assert pod_of(api, prefix)["imagePullSecrets"] == [{"name": "ghcr-pull"}]


async def test_the_workspace_claim_is_read_write_once_with_the_configured_class() -> None:
    api, _registry, provider = build()
    launch = spec()
    api.specs[launch.attempt_id] = launch
    await provider.prepare(launch)
    claim = next(row["body"] for row in api.created if row["kind"] == "persistentvolumeclaims")
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["storageClassName"] == "lab-ssd"
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"


async def test_a_bundle_above_the_configmap_cap_is_refused_rather_than_truncated() -> None:
    """08, 26: a ConfigMap has a size cap and the projected-volume form above it is not
    implemented. A truncated identity bundle is a worker given the wrong contract."""
    api, _registry, provider = build(config=KubernetesConfig(poll_interval_seconds=0))
    launch = spec()
    launch.contract["objective"] = "x" * (1024 * 1024 + 1)
    api.specs[launch.attempt_id] = launch
    with pytest.raises(Exception, match="above the ConfigMap cap"):
        await provider.prepare(launch)
