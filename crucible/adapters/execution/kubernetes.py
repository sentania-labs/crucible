"""The Kubernetes execution provider (08, 26).

One Job per role per attempt in the workers namespace, a PersistentVolumeClaim as the
workspace, the identity bundle as a ConfigMap, the harness credential as a per-attempt
Secret seeded from the harness's own Secret, and a per-attempt NetworkPolicy in place of
the egress proxy. No Docker socket anywhere, and no call outside the one namespace the
supervisor's ServiceAccount is bound to.

Everything above the provider is the Docker provider's, unchanged: the same identity
bundle, the same preparer, collector, bundle-verifier and verifier scripts, the same
reading of what they wrote, and the same credential rules (12). What differs is only
how a container is created and how bytes come back out of a workspace:

- a Job and a Pod instead of a container, with the pod shape of 26 enforced by this
  provider and again by the namespace's Pod Security admission;
- a NetworkPolicy instead of `HTTPS_PROXY` and a Squid allowlist;
- a short-lived **reader Pod** instead of a shared artifact volume. The Crucible pods
  never mount a workspace claim (26), so the collected output comes back as a tar on an
  exec stream, and so does a rotated auth file. Neither ever passes through a Pod log:
  the kubelet writes those to the node's disk, and a credential on a node is exactly
  what 12 forbids.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from crucible.adapters.execution import identity as identity_bundle
from crucible.adapters.execution import k8sspec, scripts, workspace
from crucible.adapters.execution.collected import read_outputs, read_verifications
from crucible.adapters.execution.create_policy import image_allowed
from crucible.adapters.execution.k8sapi import (
    ExecResult,
    KubernetesApiError,
    KubernetesClient,
)
from crucible.adapters.execution.k8sregistry import (
    RegistryClient,
    RegistryError,
    auths_from_dockerconfigjson,
)
from crucible.adapters.execution.k8sspec import (
    EgressPlan,
    Limits,
    Mount,
    PeerSelector,
    PodRequest,
    SpecError,
)
from crucible.adapters.execution.logstream import chunks as _chunks
from crucible.adapters.harness.registry import default_registry
from crucible.application.harnesses import (
    HarnessRegistry,
    check_image_version,
    egress_allowlist,
)
from crucible.contracts.completion_claim import CompletionClaimV1
from crucible.domain.cluster_egress import ClusterEgress, parse_cluster_egress
from crucible.domain.exit_class import ExitClass
from crucible.domain.ids import new_id
from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
    CleanupPolicy,
    CollectedArtifact,
    CollectedOutputs,
    CredentialFileSync,
    CredentialSync,
    Handle,
    ImageInfo,
    IsolationLevel,
    LaunchRefusedError,
    LaunchSpec,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    ProbeRequest,
    ProbeResult,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    VerificationRun,
    Workspace,
    WorkspaceState,
)
from crucible.ports.harness import AuthFile, CredentialSpec, ExitInfo, LaunchContext, MountMode

log = logging.getLogger("crucible.provider.kubernetes")


def _inside_declared_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, declared: str
) -> bool:
    candidate = ipaddress.ip_network(declared)
    return (
        network.version == candidate.version
        and int(network.network_address) >= int(candidate.network_address)
        and int(network.broadcast_address) <= int(candidate.broadcast_address)
    )


PROVIDER_NAME = "kubernetes"

# A name to the addresses a NetworkPolicy may name.
Resolver = Callable[[str], list[str]]

# What the canary resolves to prove cluster DNS works: a name every cluster serves, looked
# up through the pod's search path so the cluster domain need not be known here.
CANARY_DNS_NAME = "kubernetes.default.svc"

# The runtime settings source: the `kubernetes.egress` document (None when it was never
# saved) and the enabled local endpoint URL (None when no local model is enabled).
SettingsSource = Callable[[], tuple[Mapping[str, Any] | None, str | None]]

# Where `prepare` records which ConfigMap key is which bundle file, so `launch` projects
# every file back to the relative path the bundle hash covers.
ANNOTATION_IDENTITY_PATHS = "crucible.io/identity-paths"

# What a role Job's exit is when Crucible never got one: the API call failed, or the
# wait ran out and the Job was deleted. Distinct from any code a container can exit
# with, so a caller can tell "it failed" from "it never finished" (the Docker provider's
# two sentinels, with the same values, because the callers are the same code).
JOB_API_ERROR = -1
JOB_TIMED_OUT = -2

# One attempt may create a preparer, worker, collector, bundle-verifier and verifier Job.
# A ResourceQuota's Job count therefore needs this conversion before it can truthfully be
# shown as attempt capacity.
JOBS_PER_ATTEMPT = 5

# Deleting a Pod is the only way to signal one. Kubernetes sends SIGTERM, waits the
# grace period and then SIGKILLs, and the Pod object is gone either way, so the exit
# code the container produced is gone with it. These are the codes the Docker provider
# records for the same two acts, and `observe` falls back to them only for a Pod that
# Crucible itself terminated (16).
DRAIN_EXIT_CODE = 143
KILL_EXIT_CODE = 137

# 26's object table names the Jobs; `crucible.role` keeps the Docker provider's role
# names, so one label means the same thing on both providers. The two differ for three
# roles and that is the whole mapping.
OBJECT_PREFIX: dict[str, str] = {
    k8sspec.ROLE_PREPARER: "prepare",
    k8sspec.ROLE_WORKER: "worker",
    k8sspec.ROLE_COLLECTOR: "collect",
    k8sspec.ROLE_BUNDLE: "verify-bundle",
    k8sspec.ROLE_VERIFIER: "verifier",
    k8sspec.ROLE_PUBLISHER: "publish",
    k8sspec.ROLE_CLEANER: "cleaner",
    k8sspec.ROLE_READER: "reader",
}

# 26 is unconditional: "GitHub is not reachable from a worker; the preparer and the
# publisher do the git traffic." A policy document may still name these in its
# `egress_allowlist` for the roles that do need them (05b's default does), so the worker
# role subtracts them rather than trusting the list. The removal is recorded in the
# policy's annotation, so what was asked for and what was granted are both readable.
WORKER_DENIED_HOSTS: frozenset[str] = frozenset({"github.com", "api.github.com"})

DEFAULT_IMAGE_ALLOWLIST: tuple[str, ...] = (
    "crucible-worker:*",
    "ghcr.io/sentania-labs/crucible-worker:*",
)

# How much of one file the reader Pod will hand back. A worker owns its credential copy
# and can leave anything at that path, so the read is bounded before it is parsed (12).
CREDENTIAL_READ_LIMIT = 1024 * 1024
# How much of a collected output tar is accepted. The tree is excluded from it, so this
# is the diff, the bundle, the report copy and the verifier logs.
OUTPUT_READ_LIMIT = 256 * 1024 * 1024

# Pod phases that are not a running worker but not a loss either.
_PENDING_PHASES = frozenset({"Pending"})
# `status.reason` values that mean the Pod is gone because the cluster took it (26).
_LOST_REASONS = frozenset({"Evicted", "NodeShutdown", "Shutdown", "NodeAffinity", "NodeLost"})


class CollectionFailedError(ProviderError):
    """The collector could not produce the outputs. The attempt is an `environment`
    failure and nothing is collected from it (16)."""


HarnessRefusedError = LaunchRefusedError


@dataclass(frozen=True, slots=True)
class KubernetesConfig:
    """Everything the provider needs that is not on the launch spec."""

    namespace: str = "crucible-workers"
    service_account: str = "crucible-worker"
    storage_class: str = ""
    workspace_size: str = "20Gi"
    image_pull_secret: str | None = None
    # A cluster-side PVC holding the reference cache the preparer clones from. Without
    # one the preparer clones from the remote directly, as the Docker provider does
    # with `use_reference_cache` off.
    cache_claim: str | None = None
    # 26: a Pod Pending longer than this is a launch failure with the Pod's conditions
    # as the detail (image pull, no schedulable node, PVC unbound), never a stall.
    launch_timeout_seconds: int = 300
    prepare_timeout_seconds: int = 900
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    report_size_cap_bytes: int = 10 * 1024 * 1024
    log_tail_bytes: int = 64 * 1024
    max_concurrency: int = 3
    poll_interval_seconds: float = 2.0
    api_timeout_seconds: float = 30.0
    # The cluster's DNS service address. 26 allows port 53 on this address and nothing
    # else on it; every other destination inside the cluster stays denied.
    cluster_dns_ip: str = "10.96.0.10"
    # The cluster resolver and an in-cluster local endpoint as namespace and pod
    # selectors (crucible#91), which a CNI that translates a service address before it
    # evaluates policy still matches. Seeded from the settings file and replaced at
    # runtime by the `kubernetes.egress` admin setting.
    egress: ClusterEgress = field(default_factory=ClusterEgress)
    # Crucible's own namespace. No selector may name it or the workers namespace.
    control_namespace: str = "crucible"
    # The enabled local model endpoint the readiness canary proves a connection to,
    # from the routing policy in force. Empty when no local model is enabled.
    local_endpoint_url: str = ""
    # How often the runtime settings above are read back from the database, so the
    # supervisor follows an edit made through the API process without a restart.
    settings_refresh_seconds: float = 15.0
    denied_cidrs: tuple[str, ...] = k8sspec.DEFAULT_DENIED_CIDRS
    local_endpoint_cidrs: tuple[str, ...] = ()
    # 26: the allowlist is "resolved to CIDRs or FQDN rules where the CNI supports
    # them". A plain `networking.k8s.io/v1` CNI has no FQDN rule, so the names are
    # resolved here and the policy carries their addresses. Turning this off gives the
    # broad rule instead ("the public internet on 443, minus every denied range"), which
    # a deployment may want when its CNI enforces names some other way; it is off by
    # default because that rule would let a worker reach GitHub, and 26 says it cannot.
    broad_egress: bool = False
    # How long a resolved address stays in a policy before it is looked up again.
    resolve_ttl_seconds: float = 300.0
    extra_image_allowlist: tuple[str, ...] = ()
    # The harness credential Secrets in the workers namespace, by harness name (12, 26).
    credential_secrets: Mapping[str, str] = field(default_factory=dict)
    # The operator's declared mount mode per harness, as the Docker configuration
    # carries it. It may raise the adapter's minimum and never lowers it (25 step 7).
    credential_modes: Mapping[str, MountMode] = field(default_factory=dict)
    # Worker image repositories `list_images` reports the promoted tags of (25). Bare
    # repositories: the listing appends each tag the registry returns.
    image_repositories: tuple[str, ...] = ()
    # One exact, pullable worker image for the readiness canary (26). It is deliberately
    # not an entry of `image_repositories`: the listing would take it for a repository,
    # list the same tags a second time, and then build `<repo>:<probe tag>:<tag>` for
    # each of them, which is a reference that does not parse and one suppressed registry
    # round trip per tag on every `GET /admin/images`.
    probe_image: str = ""
    use_reference_cache: bool = True

    def credential_secret_name(self, harness: str) -> str:
        return self.credential_secrets.get(harness) or f"crucible-harness-{harness}"


@dataclass(frozen=True, slots=True)
class NamespaceProbe:
    """The namespace readiness probe of 26: a canary Pod that must fail to reach the API
    server, and the node's pod PID limit.

    Both are facts about the cluster, not about an attempt, so they are probed once and
    shown on the status page (25). The provider refuses to launch until the probe has
    passed, because a namespace whose CNI does not enforce egress NetworkPolicy gives a
    worker the API server, and a node with no pod PID limit gives it a fork bomb."""

    passed: bool
    egress_enforced: bool
    pid_limit: int | None
    detail: str = ""
    checked: bool = True
    # What the canary found under the same egress rules a worker gets (crucible#91):
    # whether a cluster name resolved, and whether the configured local endpoint took a
    # TCP connection. None is "not checked" (no local endpoint) or "could not tell".
    dns_resolves: bool | None = None
    local_endpoint_reachable: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace_ready": self.passed,
            "egress_enforced": self.egress_enforced,
            "dns_resolves": self.dns_resolves,
            "local_endpoint_reachable": self.local_endpoint_reachable,
            "pod_pid_limit": self.pid_limit,
            "runtime_class": "standard",
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _CredentialCopy:
    """What was seeded for one attempt: the adapter's spec, the harness Secret it came
    from, the effective mode, and the sha256 of each file as seeded (in memory only)."""

    spec: CredentialSpec
    source_secret: str
    mode: MountMode
    seeded: dict[str, str | None] = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return self.mode is MountMode.RW_NARROW


@dataclass(slots=True)
class _Launched:
    job_name: str
    spec: LaunchSpec
    image_digest: str
    limits: Limits
    network_policy: str | None = None
    credential: _CredentialCopy | None = None
    pod_name: str | None = None
    node: str | None = None
    launched_at: float = 0.0
    # What Crucible itself did to the Pod, so a Pod that is gone because Crucible
    # deleted it is never reported as lost (16).
    terminated: str | None = None
    exit_code: int | None = None


# What an adopted attempt's `_Launched` carries before the supervisor hands the real
# launch spec back on the next collect. It names nothing and runs nothing.
_ADOPTED_SPEC = LaunchSpec(
    attempt_id="",
    task_id="",
    external_id="",
    role="adopted",
    harness="",
    model="",
    image="",
    timeout_seconds=0,
    contract={},
)


class KubernetesProvider:
    """The provider of 08 on Jobs and Pods, as 26 specifies it."""

    name = PROVIDER_NAME

    def __init__(
        self,
        config: KubernetesConfig,
        client: KubernetesClient,
        registry: RegistryClient,
        harnesses: HarnessRegistry | None = None,
        resolver: Resolver | None = None,
        settings_source: SettingsSource | None = None,
    ) -> None:
        self.config = config
        # The runtime settings (the `kubernetes.egress` admin setting and the enabled
        # local endpoint), read back from the database; None in the unit tier.
        self._settings_source = settings_source
        self._settings_read_at: float | None = None
        self._file_egress = config.egress
        # Injected so the unit tier resolves without a network and the e2e tier can
        # point at the cluster's own DNS.
        self.resolve = resolver or _resolve_host
        self.client = client
        self.registry = registry
        self.harnesses = harnesses or default_registry()
        self._launched: dict[str, _Launched] = {}
        self._images: dict[str, ImageInfo] = {}
        self.last_error: dict[str, str] = {}
        self.probe: NamespaceProbe | None = None
        self._probe_lock = asyncio.Lock()
        self._quota_concurrency: int | None = None
        self._pull_auths_loaded = False
        self._resolved: dict[str, tuple[float, tuple[str, ...]]] = {}

    # ----- helpers -----------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _labels(self, spec: LaunchSpec, role: str) -> dict[str, str]:
        return k8sspec.labels(spec, role)

    def _limits(self, spec: LaunchSpec) -> Limits:
        return k8sspec.limits_from_policy(spec.policy)

    def _image_allowlist(self, spec: LaunchSpec) -> list[str]:
        return [
            str(p)
            for p in (spec.policy.get("images", {}).get("allowlist") or DEFAULT_IMAGE_ALLOWLIST)
        ] + list(self.config.extra_image_allowlist)

    # ----- images ------------------------------------------------------

    async def _resolve_image(self, spec: LaunchSpec) -> str:
        """Resolve the tag to a digest through the registry and refuse what the policy
        or the adapter's tested range does not allow (07, 13, 26).

        Only the reference-to-digest-and-labels mapping is cached, because that is a
        fact about the registry. Whether *this* attempt may run it is a question about
        this attempt's policy and is asked again on every launch: the same image under
        a different policy has to be refused, and a cache hit must not be a way past
        that."""
        cached = self._images.get(spec.image)
        if cached is None:
            await self._load_pull_auths()
            try:
                cached = await self._call(self.registry.resolve, spec.image)
            except RegistryError as exc:
                raise ProviderError(f"image {spec.image!r} is not available: {exc}") from exc
            self._images[spec.image] = cached
        if not image_allowed(spec.image, self._image_allowlist(spec)):
            raise ProviderError(f"image {spec.image!r} is outside the policy allowlist")
        check = check_image_version(self.harnesses, spec.harness, cached.labels)
        if not check.ok:
            raise HarnessRefusedError(f"refusing to launch: {check.detail}")
        return cached.reference

    async def _load_pull_auths(self) -> None:
        """The registry credential is the cluster's own image pull Secret, read once.

        Nothing about it is configuration: the deployment already has to give the
        kubelet a pull secret, and reading the same one is what keeps Crucible from
        holding a second copy of a registry password (12)."""
        if self._pull_auths_loaded or not self.config.image_pull_secret:
            return
        self._pull_auths_loaded = True
        auths = getattr(self.registry, "auths", None)
        if auths is None or not isinstance(auths, dict):
            return
        try:
            body = await self._call(self.client.get, "secrets", self.config.image_pull_secret)
        except KubernetesApiError as exc:
            log.warning("image pull secret unreadable", extra={"error": str(exc)})
            return
        raw = (body.get("data") or {}).get(".dockerconfigjson")
        if not raw:
            return
        with contextlib.suppress(RegistryError, ValueError):
            auths.update(auths_from_dockerconfigjson(base64.b64decode(str(raw))))

    # ----- the readiness probe (26) ------------------------------------

    def reload_settings(self) -> None:
        """Read the runtime settings back on the next launch or status call. The admin
        services call this after an edit; the transaction commits before either runs."""
        self._settings_read_at = None

    def apply_settings(self, document: Mapping[str, Any] | None, endpoint_url: str | None) -> None:
        """Take the `kubernetes.egress` document (None: the settings file's) and the
        enabled local endpoint. A change forgets the readiness probe, because what the
        canary proved was proved under the old rules."""
        egress = self._file_egress
        if document is not None:
            try:
                egress = parse_cluster_egress(document, protected_namespaces=self._protected())
            except ValueError as exc:
                log.error("the kubernetes.egress setting is refused: %s", exc)
                return
        updated = replace(self.config, egress=egress, local_endpoint_url=endpoint_url or "")
        if updated != self.config:
            self.config = updated
            self.probe = None

    def _protected(self) -> tuple[str, ...]:
        return tuple(n for n in (self.config.namespace, self.config.control_namespace) if n)

    async def _refresh_settings(self) -> None:
        if self._settings_source is None:
            return
        now = time.monotonic()
        if (
            self._settings_read_at is not None
            and now - self._settings_read_at < self.config.settings_refresh_seconds
        ):
            return
        self._settings_read_at = now
        try:
            document, endpoint_url = await self._call(self._settings_source)
        except Exception as exc:  # the settings file's values stay in force
            log.warning("the kubernetes runtime settings are unreadable: %s", exc)
            return
        self.apply_settings(document, endpoint_url)

    async def ensure_ready(self) -> NamespaceProbe:
        """Probe the namespace once, and keep the answer. A failed probe is re-run on
        the next call: lab-admin fixing the CNI must not need a Crucible restart."""
        await self._refresh_settings()
        if self.probe is not None and self.probe.passed:
            return self.probe
        async with self._probe_lock:
            if self.probe is not None and self.probe.passed:
                return self.probe
            self.probe = await self._run_probe()
            return self.probe

    async def _run_probe(self) -> NamespaceProbe:
        image = self._probe_image()
        if not image:
            return NamespaceProbe(
                False, False, None, "no image is configured to run the canary with", checked=False
            )
        canary_id = new_id()
        name = f"crucible-canary-{canary_id.lower()}"[:60]
        object_labels = {
            k8sspec.LABEL_ROLE: k8sspec.ROLE_CANARY,
            k8sspec.LABEL_OWNER: "crucible",
            k8sspec.LABEL_ATTEMPT: canary_id,
        }
        # The canary runs under the rules a worker gets (crucible#91): cluster DNS and
        # the local endpoint, and nothing else. What it proves is then what a worker
        # will meet, and on a CNI where those rules do not match it says so.
        endpoint_url = self.config.local_endpoint_url
        try:
            plan = await self._resolve_plan(self._local_endpoint_plan(EgressPlan(), endpoint_url))
        except (ProviderError, SpecError) as exc:
            return NamespaceProbe(
                False,
                False,
                None,
                f"local endpoint check failed: no rule can permit it ({exc})",
                checked=False,
                local_endpoint_reachable=False,
            )
        policy_name = k8sspec.object_name("np-canary", canary_id)
        policy = self._policy_body(policy_name, object_labels, canary_id, k8sspec.ROLE_CANARY, plan)
        limits = k8sspec.limits_from_policy({})
        pod = k8sspec.bare_pod(
            name=name,
            namespace=self.config.namespace,
            object_labels=object_labels,
            pod=k8sspec.pod_spec(
                PodRequest(
                    role=k8sspec.ROLE_CANARY,
                    image=image,
                    command=["sh", "-c", _CANARY_SCRIPT],
                    limits=limits,
                    env={
                        "CRUCIBLE_CANARY_DNS_NAME": CANARY_DNS_NAME,
                        **({"CRUCIBLE_CANARY_ENDPOINT_URL": endpoint_url} if endpoint_url else {}),
                    },
                    mounts=k8sspec.base_mounts(),
                    volumes=k8sspec.base_volumes(limits),
                    service_account=self.config.service_account,
                    image_pull_secret=self.config.image_pull_secret,
                )
            ),
        )
        try:
            await self._call(self.client.create, "networkpolicies", policy)
        except KubernetesApiError as exc:
            return NamespaceProbe(
                False, False, None, f"the canary NetworkPolicy was refused: {exc}", False
            )
        try:
            await self._call(self.client.create, "pods", pod)
        except KubernetesApiError as exc:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "networkpolicies", policy_name)
            return NamespaceProbe(False, False, None, f"the canary Pod was refused: {exc}", False)
        try:
            phase = await self._await_pod(name, timeout=self.config.launch_timeout_seconds)
            if phase is None:
                return NamespaceProbe(
                    False, False, None, "the canary Pod never reached a terminal phase", False
                )
            # The probe parser consumes `crucible-canary.*` keys at column one. Normal
            # worker log pulls need timestamps for resume, but the one-shot canary does
            # not, and a real API server prefixes every line when timestamps are left
            # enabled.
            body = await self._call(
                self.client.pod_log,
                name,
                container=k8sspec.CONTAINER_NAME,
                timestamps=False,
            )
            output = b"".join(frame.payload for frame in body).decode("utf-8", "replace")
        finally:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "pods", name, grace_period_seconds=0)
            await self._await_pod_gone(name)
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "networkpolicies", policy_name)
        return _read_probe(output)

    def _probe_image(self) -> str:
        """What the readiness canary runs: an image an attempt already resolved, else the
        one the deployment named, else the first configured repository.

        The last of those is a bare repository, which a kubelet reads as `:latest`, so a
        registry without that tag leaves the canary in ImagePullBackOff until the launch
        timeout and the status page reporting the namespace as not ready for a reason
        that is not about the namespace. `probe_image` is what a deployment says instead
        (C9)."""
        for image in self._images.values():
            return image.reference
        if self.config.probe_image:
            return self.config.probe_image
        for repository in self.config.image_repositories:
            return repository
        return ""

    # ----- contract ----------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        """26: `shared_disk` is false, because nothing of a workspace is ever visible to
        the Crucible process; what comes back comes back through a reader Pod."""
        return ProviderCapabilities(
            isolation=IsolationLevel.POD,
            network_control=True,
            resource_limits=True,
            shared_disk=False,
            supports_harnesses=frozenset(self.harnesses.names()),
            max_concurrency=self._quota_concurrency or self.config.max_concurrency,
        )

    def credential_available(self, harness: str) -> bool:
        return harness in self.config.credential_secrets

    async def prepare(self, spec: LaunchSpec) -> Workspace:
        repository = spec.contract.get("repository", {})
        url = spec.repository_url or str(repository.get("url", ""))
        if not url:
            raise ProviderError("the contract names no repository url")
        base_ref = str(repository.get("base_ref", "main"))
        work_branch = str(repository.get("work_branch") or f"crucible/{spec.external_id}")
        resolved = await self._resolve_image(spec)
        limits = self._limits(spec)

        # 26: the PVC, the ConfigMap and the per-attempt Secret, then the preparer Job.
        await self._delete_attempt_objects(spec.attempt_id)
        await self._create(
            "persistentvolumeclaims",
            k8sspec.workspace_claim(
                name=k8sspec.object_name("ws", spec.attempt_id),
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                size=self.config.workspace_size,
                storage_class=self.config.storage_class,
            ),
        )
        bundle, identity_paths, identity_sha = await asyncio.to_thread(
            _render_identity, spec, work_branch, self.harnesses
        )
        await self._create(
            "configmaps",
            k8sspec.config_map(
                name=k8sspec.object_name("identity", spec.attempt_id),
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                data=bundle,
                annotations={ANNOTATION_IDENTITY_PATHS: json.dumps(identity_paths, sort_keys=True)},
            ),
        )
        copy = self._credential_copy(spec)
        if copy is not None:
            await self._seed_credential(spec, copy)
        try:
            return await self._prepare_checkout(
                spec,
                url=url,
                base_ref=base_ref,
                work_branch=work_branch,
                resolved=resolved,
                limits=limits,
                identity_sha=identity_sha,
            )
        except BaseException:
            # 12: the copy is removed on *every* path, not only the clean one. Every
            # failure past this point (a preparer Job that failed, a HEAD it never
            # produced, a reader Pod that never became ready) leaves an attempt the
            # supervisor will never collect or clean up, so the seeded Secret and the
            # writable copy go now rather than waiting for a retention sweep.
            if copy is not None:
                with contextlib.suppress(Exception):
                    await self._remove_credential(spec, copy)
            raise

    async def _prepare_checkout(
        self,
        spec: LaunchSpec,
        *,
        url: str,
        base_ref: str,
        work_branch: str,
        resolved: str,
        limits: Limits,
        identity_sha: str,
    ) -> Workspace:
        repository = spec.contract.get("repository", {})
        # The shim rule is the adapter's (06): Claude Code reads AGENTS.md only where the
        # project has no CLAUDE.md of its own, and the preparer needs to be told which.
        adapter = self.harnesses.require(spec.harness)
        cache_mounts: list[Mount] = []
        cache_volumes: list[dict[str, Any]] = []
        cache_name: str | None = None
        if self.config.use_reference_cache and self.config.cache_claim:
            cache_name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            cache_mounts.append(Mount("cache", k8sspec.CACHE_MOUNT))
            cache_volumes.append(
                {
                    "name": "cache",
                    "persistentVolumeClaim": {"claimName": self.config.cache_claim},
                }
            )
        git_policy = spec.policy.get("git", {})
        exit_code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_PREPARER,
            image=resolved,
            script=scripts.preparer_script(
                url=url,
                base_ref=base_ref,
                work_branch=work_branch,
                from_remote_branch=spec.role == "correct"
                or bool(repository.get("resume_from_work_branch")),
                cache_name=cache_name,
                author_name=str(git_policy.get("author_name", "crucible-worker")),
                author_email=str(
                    git_policy.get("author_email", "crucible-worker@users.noreply.github.com")
                ),
                origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
                claude_md_wins=adapter.capabilities().claude_md_wins,
                shims=workspace.SHIM_NAMES,
                exclude_entries=workspace.EXCLUDE_ENTRIES,
                identity_mount=IDENTITY_MOUNT,
            ),
            mounts=[Mount("ws", WORK_MOUNT), *cache_mounts],
            volumes=[self._claim_volume(spec.attempt_id), *cache_volumes],
            limits=limits,
            timeout=self.config.prepare_timeout_seconds,
            plan=self._egress_plan(spec, k8sspec.ROLE_PREPARER),
        )
        if exit_code != 0:
            raise ProviderError(
                f"the preparer Job could not build the checkout (exit {exit_code}): "
                f"{self.last_error.get(k8sspec.ROLE_PREPARER, '')}"
            )
        prepared = await self._read_files(
            spec, ["output/prepared-head.txt", "output/started-from.txt"], limits
        )
        head = (prepared.get("output/prepared-head.txt") or b"").decode("utf-8", "replace").strip()
        if not head:
            raise ProviderError("the preparer produced no HEAD")
        root = f"k8s://{self.config.namespace}/{k8sspec.object_name('ws', spec.attempt_id)}"
        return Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=f"{root}/repo",
            identity_path=f"{root}/identity",
            report_path=f"{root}/report",
            output_path=f"{root}/output",
            identity_sha256=identity_sha,
            work_branch=work_branch,
            started_from=(prepared.get("output/started-from.txt") or b"")
            .decode("utf-8", "replace")
            .strip(),
        )

    async def launch(self, ws: Workspace, spec: LaunchSpec) -> Handle:
        probe = await self.ensure_ready()
        if not probe.passed:
            # 26: a namespace whose egress enforcement or pod PID limit is not proven
            # does not run a worker. This is a refusal, not a retry: the next attempt
            # would meet the same cluster.
            raise HarnessRefusedError(
                f"refusing to launch: the workers namespace is not ready ({probe.detail})"
            )
        resolved = await self._resolve_image(spec)
        limits = self._limits(spec)
        copy = self._credential_copy(spec)
        plan = self._egress_plan(spec, k8sspec.ROLE_WORKER)
        policy_name: str | None = None
        identity_paths = await self._identity_paths(spec.attempt_id)
        credential_keys = await self._credential_keys(spec.attempt_id) if copy else []
        job_name = k8sspec.object_name("worker", spec.attempt_id)
        try:
            policy_name = await self._apply_policy(spec, k8sspec.ROLE_WORKER, plan)
            body = k8sspec.job(
                name=job_name,
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                pod=self._worker_pod(
                    ws,
                    spec,
                    resolved=resolved,
                    limits=limits,
                    copy=copy,
                    identity_paths=identity_paths,
                    credential_keys=credential_keys,
                ),
                active_deadline_seconds=max(60, spec.timeout_seconds + limits.grace_seconds),
            )
            await self._create("jobs", body)
        except (KubernetesApiError, SpecError) as exc:
            with contextlib.suppress(Exception):
                await self._call(self.client.delete, "jobs", job_name)
            if policy_name:
                with contextlib.suppress(Exception):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            # 12: the per-attempt Secret exists from `prepare`; a launch that never
            # started must not leave it behind for nothing to come back for.
            with contextlib.suppress(Exception):
                await self._delete_credential_secret(spec.attempt_id)
            raise ProviderError(f"could not start the worker: {exc}") from exc
        self._launched[spec.attempt_id] = _Launched(
            job_name=job_name,
            spec=spec,
            image_digest=resolved,
            limits=limits,
            network_policy=policy_name,
            credential=copy,
            launched_at=time.monotonic(),
        )
        return Handle(
            provider=self.name,
            ref=job_name,
            attempt_id=spec.attempt_id,
            image_digest=resolved,
            name=job_name,
        )

    async def observe(self, h: Handle) -> Observation:
        launched = self._launched.get(h.attempt_id)
        try:
            job = await self._call(self.client.get, "jobs", h.ref)
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"reading the Job failed: {exc}") from exc
            job = {}
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError as exc:
            # Nothing is decided from a failed look. The supervisor logs this and asks
            # again on the next tick, which is what an attempt whose state could not be
            # read deserves.
            raise ProviderError(f"could not read the Pod of {h.ref}: {exc}") from exc
        if pod is None:
            return self._observation_without_pod(h, launched, job)
        status = pod.get("status") or {}
        phase = str(status.get("phase", ""))
        if launched is not None and not launched.pod_name:
            launched.pod_name = str((pod.get("metadata") or {}).get("name") or "")
            launched.node = str((pod.get("spec") or {}).get("nodeName") or "") or None
        terminated = _terminated_state(status)
        if terminated is not None:
            code = int(terminated.get("exitCode", -1))
            if launched is not None:
                launched.exit_code = code
            oom = str(terminated.get("reason", "")) == "OOMKilled"
            detail = str(terminated.get("reason", "")) or phase
            return Observation(
                ObservationState.EXITED,
                exit_code=code,
                detail=f"{detail}:oom_killed" if oom else detail,
                oom_killed=oom,
            )
        if phase == "Failed" and str(status.get("reason", "")) in _LOST_REASONS:
            # 26: an evicted Pod, or one whose node is gone, is `lost`, not an exit.
            return Observation(ObservationState.LOST, detail=f"the Pod was {status.get('reason')}")
        if phase == "Failed":
            # The Pod failed without the worker's own container producing an exit. The
            # init container that seeds a `rw-narrow` credential copy is the way this
            # happens in practice: it fails, the main container never starts, and only
            # the init status carries a terminated state. Reporting `running` here would
            # hang the attempt forever with nothing to classify, collect or clean up.
            init = _terminated_init(status)
            detail = str(status.get("message") or status.get("reason") or "")
            if init is not None:
                return Observation(
                    ObservationState.EXITED,
                    exit_code=70,
                    detail=(
                        f"the {init.get('containerName', 'init')} container failed before "
                        f"the worker started (exit {init.get('exitCode')}, "
                        f"{init.get('reason', '')}) {detail}".strip()
                    ),
                )
            return Observation(
                ObservationState.EXITED,
                exit_code=70,
                detail=f"the Pod failed before the worker started: {detail or phase}",
            )
        if phase in _PENDING_PHASES and self._pending_too_long(launched):
            return self._pending_failure(pod)
        return Observation(ObservationState.RUNNING, detail=phase or "Pending")

    def _observation_without_pod(
        self, h: Handle, launched: _Launched | None, job: Mapping[str, Any]
    ) -> Observation:
        """A Job whose Pod is gone. What that means depends on who removed it."""
        if launched is not None and launched.exit_code is not None:
            # The exit was already observed; the Pod being reaped afterwards is not a
            # second event.
            return Observation(
                ObservationState.EXITED, exit_code=launched.exit_code, detail="pod removed"
            )
        if launched is not None and launched.terminated is not None:
            code = DRAIN_EXIT_CODE if launched.terminated == "drain" else KILL_EXIT_CODE
            launched.exit_code = code
            return Observation(
                ObservationState.EXITED,
                exit_code=code,
                detail=f"the Pod was deleted by Crucible ({launched.terminated})",
            )
        for condition in (job.get("status") or {}).get("conditions") or []:
            if not isinstance(condition, dict) or str(condition.get("status")) != "True":
                continue
            if str(condition.get("type")) == "Complete":
                # The Job finished and its Pod was garbage collected afterwards (a node
                # drain, an operator, a TTL controller someone adds). A successful
                # attempt whose Pod was reaped is not a loss.
                return Observation(ObservationState.EXITED, exit_code=0, detail="the Job completed")
            if str(condition.get("reason")) == "DeadlineExceeded":
                # The Job's own deadline fired. Crucible drains before it (26), so this
                # is the cluster killing a worker Crucible had not classified yet.
                return Observation(
                    ObservationState.EXITED,
                    exit_code=KILL_EXIT_CODE,
                    detail="the Job deadline killed the Pod",
                )
        if not job:
            return Observation(ObservationState.LOST, detail="the namespace has no such Job")
        if launched is not None and self._pending_too_long(launched):
            return self._pending_failure(None)
        return Observation(ObservationState.LOST, detail="the Job has no Pod")

    def _pending_too_long(self, launched: _Launched | None) -> bool:
        if launched is None or not launched.launched_at:
            return False
        return time.monotonic() - launched.launched_at > self.config.launch_timeout_seconds

    def _pending_failure(self, pod: Mapping[str, Any] | None) -> Observation:
        """26: Pending past the launch timeout is a launch failure with the Pod's
        conditions as detail, not a stall.

        It is reported as exit 70, which 16 classifies as `environment`: the provider
        failed before the harness ran, the attempt retries if the policy allows it, and
        the supervisor tick keeps moving. Raising here instead would make one unschedulable
        Pod an exception on every tick for as long as it stayed unschedulable."""
        conditions = []
        for condition in ((pod or {}).get("status") or {}).get("conditions") or []:
            if isinstance(condition, dict):
                conditions.append(
                    f"{condition.get('type')}={condition.get('status')}"
                    f"({condition.get('reason') or ''}: {condition.get('message') or ''})"
                )
        for entry in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
            waiting = (entry.get("state") or {}).get("waiting") if isinstance(entry, dict) else None
            if waiting:
                conditions.append(f"waiting({waiting.get('reason')}: {waiting.get('message')})")
        detail = "; ".join(conditions) or "the Pod did not start and reported no condition"
        return Observation(
            ObservationState.EXITED,
            exit_code=70,
            detail=f"the Pod stayed Pending past the launch timeout: {detail}",
        )

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError:
            return []
        if pod is None:
            return []
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            frames = await self._call(
                self.client.pod_log,
                name,
                container=k8sspec.CONTAINER_NAME,
                since_time=since.timestamp,
            )
        except KubernetesApiError as exc:
            if exc.status == 404:
                return []
            raise ProviderError(f"log pull failed: {exc}") from exc
        return _chunks(frames, since)

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        launched = self._launched.get(h.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            raise ProviderError("collect needs the launch spec and the provider has none")
        # The stored launch spec is authoritative after a supervisor restart. An
        # adopted provider only has the live Pod shape until the supervisor supplies
        # this spec again for collection.
        limits = self._limits(spec)
        repository = spec.contract.get("repository", {})
        work_branch = ws.work_branch or str(
            repository.get("work_branch") or f"crucible/{spec.external_id}"
        )
        # 12: the credential copy is read back and removed before anything else runs.
        credential_sync = await self._sync_credential(h, spec, limits)
        stdout_tail, stderr_tail = await self._worker_tails(h)
        observation = await self.observe(h)
        adapter = self.harnesses.get(spec.harness)
        quota_checkpoint = bool(
            adapter is not None
            and observation.state is ObservationState.EXITED
            and adapter.classify_exit(
                ExitInfo(exit_code=observation.exit_code), stdout_tail, stderr_tail, None
            )
            is ExitClass.QUOTA_EXHAUSTED
        )
        collector_exit = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_COLLECTOR,
            image=launched.image_digest if launched else spec.image,
            script=scripts.collector_script(
                base_ref=str(repository.get("base_ref", "main")),
                work_branch=work_branch,
                size_cap_bytes=self.config.report_size_cap_bytes,
                quota_attempt_id=spec.attempt_id if quota_checkpoint else None,
            ),
            mounts=[
                Mount("ws", REPO_MOUNT, read_only=not quota_checkpoint, sub_path="repo"),
                Mount("ws", REPORT_MOUNT, read_only=True, sub_path="report"),
                Mount("ws", OUTPUT_MOUNT, sub_path="output"),
            ],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=limits,
            timeout=self.config.collector_timeout_seconds,
            plan=EgressPlan(),
        )
        if collector_exit in (JOB_TIMED_OUT, JOB_API_ERROR):
            raise CollectionFailedError(
                self.last_error.get(k8sspec.ROLE_COLLECTOR)
                or f"the collector did not finish within {self.config.collector_timeout_seconds}s"
            )
        bundle_ok = False
        if collector_exit == 0:
            bundle_ok = (
                await self._run_role_job(
                    spec,
                    role=k8sspec.ROLE_BUNDLE,
                    image=launched.image_digest if launched else spec.image,
                    script=scripts.BUNDLE_VERIFY_SCRIPT,
                    mounts=[Mount("ws", OUTPUT_MOUNT, read_only=True, sub_path="output")],
                    volumes=[self._claim_volume(spec.attempt_id)],
                    limits=limits,
                    timeout=120,
                    plan=EgressPlan(),
                )
                == 0
            )
        verifications = await self._run_verifier(spec, limits)
        with tempfile.TemporaryDirectory(prefix="crucible-k8s-") as scratch:
            root = Path(scratch)
            await self._read_workspace(spec, root, limits)
            outputs = read_outputs(
                root / "output",
                root / "verify",
                spec=spec,
                bundle_verified=bundle_ok,
                collector_exit=collector_exit,
                verifications=_merge_verifications(
                    verifications, root / "verify", spec, self.config.verifier_timeout_seconds
                ),
                tail_bytes=self.config.log_tail_bytes,
            )
        state = await self._workspace_state(spec.attempt_id)
        return CollectedOutputs(
            report=outputs.report,
            report_raw=outputs.report_raw,
            blocked_md=outputs.blocked_md,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            diff_paths=outputs.diff_paths,
            diff_text=outputs.diff_text,
            bundle=outputs.bundle,
            artifacts=(*outputs.artifacts, self._launch_evidence(spec, observation)),
            verifications=outputs.verifications,
            workspace_state=state,
            copy_rejections=outputs.copy_rejections,
            credential_sync=credential_sync,
            checkpoint_refusal=outputs.checkpoint_refusal,
        )

    def _launch_evidence(self, spec: LaunchSpec, observation: Observation) -> CollectedArtifact:
        """26's observability list, as one artifact of the attempt.

        The image digest is already on the attempt row (the Handle carries it). The rest
        of what 26 asks an attempt to record, the Job and Pod names, the node, the
        effective limits, the pod PID limit and the NetworkPolicy applied, has no field
        of its own on the port, so it is recorded as evidence the way every other
        per-attempt fact Crucible observed is: a stored artifact with an
        `artifact_present` evidence row (11). Nothing in it is a value."""
        launched = self._launched.get(spec.attempt_id)
        probe = self.probe
        document = {
            "provider": self.name,
            "namespace": self.config.namespace,
            "image_digest": launched.image_digest if launched else "",
            "job": launched.job_name if launched else "",
            "pod": (launched.pod_name if launched else "") or "",
            "node": (launched.node if launched else "") or "",
            "limits": self._limits(spec).as_dict(),
            "pod_pid_limit": probe.pid_limit if probe else None,
            "runtime_class": "standard",
            "network_policy": (launched.network_policy if launched else None),
            "egress": list(self._egress_plan(spec, k8sspec.ROLE_WORKER).hosts),
            "final_observation": {
                "state": observation.state.value,
                "exit_code": observation.exit_code,
                "detail": observation.detail,
            },
        }
        return CollectedArtifact(
            name="report/kubernetes-launch.json",
            type="run_evidence",
            content=json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        """26: drain deletes the Pod with the policy grace period, kill with grace zero.

        Deleting a Pod is the only signal Kubernetes offers. What Crucible did is
        remembered, so a Pod that is gone because Crucible deleted it is reported as an
        exit and never as a loss (16)."""
        launched = self._launched.get(h.attempt_id)
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError as exc:
            raise ProviderError(f"terminate could not read the Pod of {h.ref}: {exc}") from exc
        grace = launched.limits.grace_seconds if launched else 60
        if launched is not None:
            launched.terminated = mode
        if pod is None:
            return
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            await self._call(
                self.client.delete,
                "pods",
                name,
                grace_period_seconds=grace if mode == "drain" else 0,
            )
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"terminate failed: {exc}") from exc

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """12: remove anything secret placed for an attempt that will never be
        collected. The per-attempt Secret always, and the writable copy on the claim
        when one was seeded. Nothing else of the workspace is touched."""
        await self._delete_credential_secret(ws.attempt_id)
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            return
        copy = launched.credential if launched is not None else self._credential_copy(spec)
        if copy is None or not copy.writable:
            return
        with contextlib.suppress(Exception):
            await self._remove_from_claim(spec, [k8sspec.CREDENTIAL_LEAF])

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None:
        """08, 26: only ever called for an attempt that recorded `logs_drained`.

        Jobs and the NetworkPolicy go; the per-attempt Secret goes under every policy,
        `keep` included (12, 16); the claim is kept or deleted per policy, and a kept
        claim carries a retention label the sweep honours."""
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        await self._delete_by_label(("jobs", "networkpolicies", "pods"), attempt_id=ws.attempt_id)
        await self._delete_credential_secret(ws.attempt_id)
        if policy is CleanupPolicy.DELETE:
            await self._delete_by_label(
                ("persistentvolumeclaims", "configmaps"), attempt_id=ws.attempt_id
            )
        else:
            leaves = (
                ["repo", "output/tree", k8sspec.CREDENTIAL_LEAF]
                if policy is CleanupPolicy.KEEP_DIFF_ONLY
                # The checkout, the verifier's tree and any credential copy go; the
                # collected evidence stays on the claim (08).
                else [k8sspec.CREDENTIAL_LEAF]
            )
            if spec is None:
                log.warning(
                    "a retained claim keeps its credential leaf: no launch spec to remove it with",
                    extra={"attempt_id": ws.attempt_id},
                )
            else:
                try:
                    await self._remove_from_claim(spec, leaves)
                except Exception as exc:
                    # 12: a rotated token left on a retained claim is the thing this
                    # call exists to stop. A cleanup that could not do it says so
                    # rather than reporting success.
                    log.warning(
                        "a retained claim may still hold the credential copy",
                        extra={"attempt_id": ws.attempt_id, "error": str(exc)},
                    )
            with contextlib.suppress(KubernetesApiError):
                await self._call(
                    self.client.patch,
                    "persistentvolumeclaims",
                    k8sspec.object_name("ws", ws.attempt_id),
                    {"metadata": {"labels": {k8sspec.LABEL_RETAIN: policy.value}}},
                )
        self._launched.pop(ws.attempt_id, None)

    async def reconcile(self) -> list[Handle]:
        """Adopt by label (10, 26). Only Jobs whose Pod is alive are handles."""
        try:
            rows = await self._call(
                self.client.list_objects,
                "jobs",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ROLE: k8sspec.ROLE_WORKER}),
            )
        except KubernetesApiError as exc:
            raise ProviderError(f"reconcile failed: {exc}") from exc
        handles: list[Handle] = []
        for row in rows:
            metadata = row.get("metadata") or {}
            attempt_id = str((metadata.get("labels") or {}).get(k8sspec.LABEL_ATTEMPT, ""))
            name = str(metadata.get("name", ""))
            if not attempt_id or not name:
                continue
            try:
                pod = await self._pod_of(name)
            except KubernetesApiError:
                continue
            # A Job's `status.active` lags its Pod, so the Pod is what says whether a
            # worker is alive: 08 adopts only what is actually running.
            if pod is None:
                continue
            phase = str((pod.get("status") or {}).get("phase", ""))
            if phase not in ("Pending", "Running"):
                continue
            if attempt_id not in self._launched:
                # A restarted supervisor has no memory of the launch, so the Pending
                # timeout of 26 would never fire for an adopted Pod that will never
                # schedule. The clock comes from the Job's own creation timestamp, not
                # from now, so a Pod that has already been Pending too long is caught on
                # the first observation rather than being given the window again.
                created = _age_seconds(str(metadata.get("creationTimestamp", "")))
                pod_spec = pod.get("spec") or {}
                containers = pod_spec.get("containers") or []
                image = str((containers[0] if containers else {}).get("image", ""))
                limits = replace(
                    k8sspec.limits_from_policy({}),
                    grace_seconds=int(pod_spec.get("terminationGracePeriodSeconds") or 60),
                )
                self._launched[attempt_id] = _Launched(
                    job_name=name,
                    spec=_ADOPTED_SPEC,
                    image_digest=image,
                    limits=limits,
                    launched_at=time.monotonic() - created,
                )
            launched = self._launched[attempt_id]
            handles.append(
                Handle(
                    provider=self.name,
                    ref=name,
                    attempt_id=attempt_id,
                    image_digest=launched.image_digest,
                    name=name,
                )
            )
        return handles

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove what is labelled for attempts Crucible no longer tracks (16)."""
        live = set(keep)
        removed = 0
        for kind in (
            "jobs",
            "pods",
            "networkpolicies",
            "secrets",
            "configmaps",
            "persistentvolumeclaims",
        ):
            try:
                rows = await self._call(
                    self.client.list_objects, kind, label_selector=k8sspec.LABEL_ATTEMPT
                )
            except KubernetesApiError:
                continue
            for row in rows:
                metadata = row.get("metadata") or {}
                labels = metadata.get("labels") or {}
                attempt_id = str(labels.get(k8sspec.LABEL_ATTEMPT, ""))
                if kind == "persistentvolumeclaims" and labels.get(k8sspec.LABEL_RETAIN):
                    # A claim a cleanup policy deliberately kept carries the retention
                    # label; the sweep honours it (26) and the workspace retention
                    # window of 16 is what removes it later.
                    continue
                if attempt_id and attempt_id not in live:
                    with contextlib.suppress(KubernetesApiError):
                        await self._call(self.client.delete, kind, str(metadata.get("name", "")))
                        removed += 1
        return removed

    async def list_images(self) -> list[ImageInfo]:
        """The promoted worker images the cluster can pull, from the registry the
        release publishes to (13, 25, 26). A cluster holds no image on Crucible's side,
        so there is nothing local to list."""
        await self._load_pull_auths()
        images: list[ImageInfo] = []
        for repository in self.config.image_repositories:
            try:
                tags = await self._call(self.registry.list_tags, repository)
            except RegistryError as exc:
                raise ProviderError(f"image listing failed for {repository}: {exc}") from exc
            for tag in tags:
                try:
                    info = await self._call(self.registry.resolve, f"{repository}:{tag}")
                except RegistryError:
                    continue
                if info.harnesses:
                    images.append(replace(info, reference=f"{repository}:{tag}"))
        return sorted(images, key=lambda i: i.reference)

    async def health(self) -> ProviderHealth:
        """25: the API server reachable, the namespace probe, the CNI egress result, the
        pod PID limit, and the runtime class in use."""
        checks: dict[str, Any] = {"namespace": self.config.namespace}
        try:
            checks["api_server"] = await self._call(self.client.version)
        except Exception as exc:
            checks["api_server"] = f"unreachable: {type(exc).__name__}"
            return ProviderHealth("unavailable", checks)
        with contextlib.suppress(Exception):
            self._quota_concurrency = await self._read_quota()
        checks["max_concurrency"] = self._quota_concurrency or self.config.max_concurrency
        probe = await self.ensure_ready()
        checks.update(probe.as_dict())
        if not probe.checked:
            return ProviderHealth("degraded", checks)
        return ProviderHealth("ok" if probe.passed else "degraded", checks)

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """25: run the hardened image with the credential mounted for one prompt under a
        hard timeout, sync the named files back, and remove everything.

        The Kubernetes form of the probe is a worker Job with no workspace claim: the
        prompt needs a credential and an identity file, not a checkout. Everything it
        creates is labelled with the probe's own id and removed in the `finally`."""
        raise ProviderError(
            "the bounded credential probe runs where the harness image and its "
            "credential directory are; on Kubernetes it is the login Job of 26, which "
            "is not part of this provider yet (C8a)"
        )

    # ----- the pod bodies ----------------------------------------------

    def _claim_volume(self, attempt_id: str, *, read_only: bool = False) -> dict[str, Any]:
        return {
            "name": "ws",
            "persistentVolumeClaim": {
                "claimName": k8sspec.object_name("ws", attempt_id),
                "readOnly": read_only,
            },
        }

    def _identity_volume(self, spec: LaunchSpec, paths: Mapping[str, str]) -> dict[str, Any]:
        """The identity bundle, key by key.

        A ConfigMap key may not hold a path separator, so `harness/settings.json`
        is stored under a flattened key and projected back to its real relative path
        here. The mapping is not guessed from the key: it is the mapping `prepare`
        recorded on the ConfigMap itself, so a template file whose own name contains
        the separator's encoding still lands where the bundle hash says it is."""
        return {
            "name": "identity",
            "configMap": {
                "name": k8sspec.object_name("identity", spec.attempt_id),
                "defaultMode": 0o444,
                "items": [{"key": key, "path": path} for key, path in sorted(paths.items())],
            },
        }

    async def _identity_paths(self, attempt_id: str) -> dict[str, str]:
        body = await self._call(
            self.client.get, "configmaps", k8sspec.object_name("identity", attempt_id)
        )
        annotations = (body.get("metadata") or {}).get("annotations") or {}
        raw = annotations.get(ANNOTATION_IDENTITY_PATHS)
        if raw:
            with contextlib.suppress(ValueError):
                loaded = json.loads(str(raw))
                if isinstance(loaded, dict):
                    return {str(k): str(v) for k, v in loaded.items()}
        return {key: key for key in (body.get("data") or {})}

    def _worker_pod(
        self,
        ws: Workspace,
        spec: LaunchSpec,
        *,
        resolved: str,
        limits: Limits,
        copy: _CredentialCopy | None,
        identity_paths: Mapping[str, str],
        credential_keys: Sequence[str],
    ) -> dict[str, Any]:
        command, launch_env = self._command(spec)
        env = {
            "CRUCIBLE_ATTEMPT_ID": spec.attempt_id,
            "CRUCIBLE_TASK_EXTERNAL_ID": spec.external_id,
            "CRUCIBLE_IDENTITY_DIR": IDENTITY_MOUNT,
            "CRUCIBLE_REPORT_DIR": REPORT_MOUNT,
            "CRUCIBLE_REPO_DIR": REPO_MOUNT,
            "HOME": "/home/worker",
            "CRUCIBLE_EGRESS_ALLOWLIST": ",".join(
                self._egress_plan(spec, k8sspec.ROLE_WORKER).hosts
            ),
            **spec.env,
            **launch_env,
        }
        mounts = [
            *k8sspec.base_mounts(),
            # 26's mount layout, with the paths the identity bundle names (06): the
            # bundle tells the worker its checkout is at REPO_MOUNT and its report
            # directory at REPORT_MOUNT, so those are where they are mounted.
            Mount("ws", REPO_MOUNT, sub_path="repo"),
            Mount("ws", REPORT_MOUNT, sub_path="report"),
            Mount("identity", IDENTITY_MOUNT, read_only=True),
        ]
        volumes = [
            *k8sspec.base_volumes(limits),
            self._claim_volume(spec.attempt_id),
            self._identity_volume(spec, identity_paths),
        ]
        init_containers: list[dict[str, Any]] = []
        if copy is not None:
            credential_mounts, credential_volumes, init_containers = self._credential_mounts(
                spec, copy, resolved, limits, credential_keys
            )
            mounts.extend(credential_mounts)
            volumes.extend(credential_volumes)
            env.update(copy.spec.env())
        return k8sspec.pod_spec(
            PodRequest(
                role=k8sspec.ROLE_WORKER,
                image=resolved,
                command=command,
                limits=limits,
                env=env,
                mounts=mounts,
                volumes=volumes,
                init_containers=init_containers,
                working_dir=REPO_MOUNT,
                service_account=self.config.service_account,
                image_pull_secret=self.config.image_pull_secret,
            )
        )

    def _credential_mounts(
        self,
        spec: LaunchSpec,
        copy: _CredentialCopy,
        image: str,
        limits: Limits,
        present: Sequence[str],
    ) -> tuple[list[Mount], list[dict[str, Any]], list[dict[str, Any]]]:
        """The per-attempt credential, mounted per the adapter's declaration (12, 26).

        Read-only is the Secret itself, which is a tmpfs the worker cannot write and
        nothing ever lands on a disk for.

        `rw-narrow` cannot be the Secret: a Kubernetes Secret volume is read-only
        whatever the mount asks for, and the harnesses that declare `rw-narrow` refresh
        their own token in place (S1). So an init container copies the named files off
        the Secret into the `credential` leaf of the attempt's own claim, mode 0700 and
        0600, which is the same shape and the same properties 12 requires and the same
        place the Docker provider puts it. The rotated file is read back from there
        through the reader Pod and the copy is removed under every cleanup policy."""
        target = copy.spec.mount_target
        templates = sorted(copy.spec.templates)
        # A Secret key may not hold a path separator and an auth file's name may (AGY's
        # token sits under `antigravity-cli/`), so the volume projects each key back to
        # the relative path the adapter declared.
        # Only the keys the Secret actually carries. An adapter may declare an optional
        # auth file (Claude Code's `.claude.json`) that the harness Secret does not
        # have, and a projection naming a key that is not there is a Pod the kubelet
        # refuses to start: the worker would sit Pending with nothing to classify.
        items = [
            {"key": _secret_key(auth.name), "path": auth.name, "mode": 0o400}
            for auth in copy.spec.auth_files
            if _secret_key(auth.name) in set(present)
        ]
        source_volume = {
            "name": "cred-source" if copy.writable else "cred",
            "secret": {
                "secretName": k8sspec.object_name("cred", spec.attempt_id),
                "defaultMode": 0o400,
                "items": items,
                # A harness whose optional auth file was absent still starts; a required
                # one refused the launch before this Pod was rendered (12).
                "optional": False,
            },
        }
        mounts: list[Mount] = []
        volumes: list[dict[str, Any]] = []
        init: list[dict[str, Any]] = []
        if copy.writable:
            mounts.append(Mount("ws", target, sub_path=k8sspec.CREDENTIAL_LEAF))
            init.append(
                {
                    "name": k8sspec.CREDENTIAL_INIT_CONTAINER,
                    "image": image,
                    "command": ["sh", "-c", _seed_script(copy.spec)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "limits": {
                            "cpu": limits.cpu,
                            "memory": limits.memory,
                            "ephemeral-storage": limits.ephemeral_storage,
                        },
                        "requests": {"cpu": limits.cpu, "memory": limits.memory},
                    },
                    "volumeMounts": [
                        {
                            "name": "cred-source",
                            "mountPath": k8sspec.CREDENTIAL_SOURCE_MOUNT,
                            "readOnly": True,
                        },
                        {
                            "name": "ws",
                            "mountPath": "/crucible/credential",
                            "subPath": k8sspec.CREDENTIAL_LEAF,
                        },
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            )
            volumes.append(source_volume)
        else:
            mounts.append(Mount("cred", target, read_only=True))
            volumes.append(source_volume)
        for name in templates:
            # 12, 13: the Crucible-owned templates, read-only, each at its own path
            # inside the credential directory, so a worker cannot plant a hook or a
            # server definition a later worker would inherit. They come from the
            # identity bundle, so the bundle hash covers them.
            mounts.append(
                Mount(
                    "identity",
                    f"{target}/{name}",
                    read_only=True,
                    sub_path=f"{k8sspec.TEMPLATE_PREFIX}/{name}",
                )
            )
        return mounts, volumes, init

    def _command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str]]:
        """The harness argv, wrapped exactly as the Docker provider wraps it (07)."""
        from crucible.adapters.execution.docker import LAUNCH_WRAPPER  # noqa: PLC0415

        argv = list(spec.command)
        if not argv:
            adapter = self.harnesses.get(spec.harness)
            if adapter is not None:
                argv = list(adapter.build_launch(self._launch_context(spec)).argv)
        wrapped = bool(spec.env_from_files or spec.stdin_files or spec.stdin_text)
        wrapped = wrapped or bool(spec.transcript_path)
        if not wrapped:
            return argv, {}
        env: dict[str, str] = {}
        if spec.env_from_files:
            env["CRUCIBLE_ENV_FROM_FILES"] = " ".join(
                f"{var}={path}" for var, path in sorted(spec.env_from_files.items())
            )
        if spec.stdin_files:
            env["CRUCIBLE_STDIN_FILES"] = " ".join(spec.stdin_files)
        if spec.stdin_text:
            env["CRUCIBLE_PROMPT"] = spec.stdin_text
        if spec.transcript_path:
            env["CRUCIBLE_TRANSCRIPT"] = spec.transcript_path
        return ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *argv], env

    def _launch_context(self, spec: LaunchSpec) -> LaunchContext:
        return LaunchContext(
            attempt_id=spec.attempt_id,
            model=spec.model,
            effort=spec.effort,
            timeout_seconds=spec.timeout_seconds,
            identity_mount=IDENTITY_MOUNT,
            report_mount=REPORT_MOUNT,
            repo_mount=REPO_MOUNT,
            credential_mounted=self._credential_copy(spec) is not None,
            endpoint=spec.endpoint,
            endpoint_url=spec.endpoint_url,
        )

    # ----- the network policy (26) --------------------------------------

    def _egress_plan(self, spec: LaunchSpec, role: str) -> EgressPlan:
        """What each role may reach (26), from the same allowlist source that generates
        the Squid configuration locally: the policy's `egress_allowlist`, the contract's
        `egress_extra`, the adapter's declared endpoints, and a local route's exact
        `endpoint_url` host and port (05b, 13, S6, S16)."""
        network_policy = str(spec.policy.get("network", {}).get("mode", "egress-proxy"))
        if spec.network == "none" or network_policy == "none":
            return EgressPlan()
        network = spec.policy.get("network", {})
        policy_hosts = [str(h) for h in (network.get("egress_allowlist") or [])]
        extra = [str(h) for h in (spec.contract.get("constraints", {}).get("egress_extra") or [])]
        if role == k8sspec.ROLE_WORKER:
            wanted = tuple(
                host
                for host in egress_allowlist(
                    self.harnesses, spec.harness, policy_hosts, extra, spec.endpoint_url
                )
                if host not in WORKER_DENIED_HOSTS
            )
        elif role in (k8sspec.ROLE_PREPARER, k8sspec.ROLE_PUBLISHER):
            # 26: the preparer and the publisher do the git traffic, and nothing else.
            # GitHub is not reachable from a worker.
            wanted = ("api.github.com", "github.com")
        elif role == k8sspec.ROLE_LOGIN:
            adapter = self.harnesses.get(spec.harness)
            wanted = tuple(sorted(adapter.capabilities().endpoints)) if adapter else ()
        elif role == k8sspec.ROLE_VERIFIER:
            # 26: the verifier gets the registries only when the policy says so. The
            # policy's own allowlist is that statement; the harness endpoints are not
            # part of it, because the verifier runs the repository's commands and never
            # a model, and the git remote is not part of it either, for the same reason
            # a worker does not get it.
            wanted = tuple(sorted(set(policy_hosts) - WORKER_DENIED_HOSTS))
        else:
            # Collector, bundle verifier, reader, cleaner: no egress at all.
            return EgressPlan()
        hosts = tuple(h for h in wanted if ":" not in h)
        endpoints = tuple(h for h in wanted if ":" in h)
        plan = EgressPlan(hosts=hosts, endpoints=endpoints)
        if role == k8sspec.ROLE_WORKER:
            plan = self._local_endpoint_plan(plan, spec.endpoint_url)
        return plan

    def _local_endpoint_plan(self, plan: EgressPlan, endpoint_url: str | None) -> EgressPlan:
        """Add the local model endpoint to a plan, in the form this cluster matches.

        Out of the cluster it is the URL's `host:port`, which `_resolve_plan` turns into
        exact addresses. In the cluster (the `kubernetes.egress` setting names its
        namespace) it is a selector on the gateway's pods and their port instead, and
        the `host:port` is taken out: it would resolve to a service address, which is
        inside a denied range and which a translating CNI never matches (crucible#91)."""
        if not endpoint_url:
            return plan
        parsed = urlsplit(endpoint_url)
        if not parsed.hostname:
            return plan
        url_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        destination = f"{parsed.hostname}:{url_port}"
        egress = self.config.egress
        if not egress.endpoint_in_cluster:
            if destination in plan.endpoints:
                return plan
            return replace(plan, endpoints=(*plan.endpoints, destination))
        return replace(
            plan,
            endpoints=tuple(e for e in plan.endpoints if e != destination),
            endpoint_selector=PeerSelector(egress.endpoint_namespace, egress.endpoint_pod_labels),
            endpoint_ports=(egress.endpoint_port or url_port,),
        )

    def _policy_body(
        self,
        name: str,
        object_labels: Mapping[str, str],
        attempt_id: str,
        role: str,
        plan: EgressPlan,
    ) -> dict[str, Any]:
        egress = self.config.egress
        protected = self._protected()
        dns_selector = None
        if egress.dns_namespace:
            dns_selector = k8sspec.check_selector(
                PeerSelector(egress.dns_namespace, egress.dns_pod_labels),
                what="cluster DNS",
                protected_namespaces=protected,
            )
        if plan.endpoint_selector is not None:
            k8sspec.check_selector(
                plan.endpoint_selector, what="local endpoint", protected_namespaces=protected
            )
        return k8sspec.egress_policy(
            name=name,
            namespace=self.config.namespace,
            object_labels=object_labels,
            attempt_id=attempt_id,
            role=role,
            plan=plan,
            dns_server=self.config.cluster_dns_ip,
            denied_cidrs=self.config.denied_cidrs,
            dns_selector=dns_selector,
        )

    async def _apply_policy(self, spec: LaunchSpec, role: str, plan: EgressPlan) -> str | None:
        """One NetworkPolicy per attempt per role that needs egress.

        26 asks for "one policy per attempt selecting that attempt's pods, with the
        role-specific egress sets". A NetworkPolicy has one podSelector, so a single
        object per attempt would have to carry the union of every role's destinations,
        which would hand the worker GitHub and the collector the model endpoints. The
        selector therefore carries the role as well, and a role with no egress gets no
        policy at all: the namespace's default deny is already the answer for it."""
        if plan.empty:
            return None
        plan = await self._resolve_plan(plan)
        name = k8sspec.object_name(f"np-{role}", spec.attempt_id)
        body = self._policy_body(name, self._labels(spec, role), spec.attempt_id, role, plan)
        await self._create("networkpolicies", body)
        return name

    async def _resolve_plan(self, plan: EgressPlan) -> EgressPlan:
        """Turn the allowlist's names into the addresses a CIDR-only CNI can enforce.

        A name that does not resolve refuses the launch rather than being dropped or
        widened, which is the Docker provider's rule for the same situation: an attempt
        whose allowlist the egress path cannot actually permit is refused, never run
        with less network than the policy promised (13)."""
        resolved_endpoints: list[str] = []
        endpoint_unresolved: list[str] = []
        endpoint_forbidden: list[str] = []
        for endpoint in plan.endpoints:
            host, separator, port = endpoint.rpartition(":")
            if not separator or not host or not port.isdigit():
                raise k8sspec.SpecError(f"{endpoint!r} is not an address:port destination")
            try:
                parsed_address = ipaddress.ip_address(host)
            except ValueError:
                addresses = tuple(await self._call(self.resolve, host))
                if not addresses:
                    endpoint_unresolved.append(host)
                    continue
                for cidr in addresses:
                    network = ipaddress.ip_network(cidr)
                    address_text = str(network.network_address)
                    denied = k8sspec.denied_by(str(network), self.config.denied_cidrs)
                    explicitly_local = any(
                        _inside_declared_network(network, value)
                        for value in self.config.local_endpoint_cidrs
                    )
                    if denied is not None and not explicitly_local:
                        endpoint_forbidden.append(
                            f"{host} resolved to {address_text} inside {denied}"
                        )
                    resolved_endpoints.append(f"{address_text}:{port}")
            else:
                cidr = f"{parsed_address}/{32 if parsed_address.version == 4 else 128}"
                denied = k8sspec.denied_by(cidr, self.config.denied_cidrs)
                if denied is not None:
                    endpoint_forbidden.append(f"{host} inside {denied}")
                resolved_endpoints.append(endpoint)
        if endpoint_unresolved:
            raise ProviderError(
                "the configured local endpoint does not resolve to an address, so no "
                f"NetworkPolicy can permit it: {sorted(endpoint_unresolved)}"
            )
        if endpoint_forbidden:
            raise ProviderError(
                "the configured local endpoint names or resolves to an address this namespace "
                f"denies: {sorted(endpoint_forbidden)}"
            )
        plan = replace(plan, endpoints=tuple(dict.fromkeys(resolved_endpoints)))
        if self.config.broad_egress or not plan.hosts:
            return replace(plan, broad=self.config.broad_egress)
        cidrs: list[str] = []
        unresolved: list[str] = []
        forbidden: list[str] = []
        now = time.monotonic()
        for host in plan.hosts:
            cached = self._resolved.get(host)
            if cached is not None and now - cached[0] < self.config.resolve_ttl_seconds:
                addresses = cached[1]
            else:
                addresses = tuple(await self._call(self.resolve, host))
                self._resolved[host] = (now, addresses)
            if not addresses:
                unresolved.append(host)
            for address in addresses:
                # 26: the API server, the node network, other namespaces, link-local and
                # the lab's private ranges are denied. A name that resolves into one of
                # them would otherwise become an allow rule for exactly the destination
                # the policy denies, whether by a vendor's split-horizon record, a CNAME
                # change, or a poisoned resolver. It refuses the launch.
                denied = k8sspec.denied_by(address, self.config.denied_cidrs)
                if denied is not None:
                    forbidden.append(f"{host} -> {address} inside {denied}")
            cidrs.extend(addresses)
        if unresolved:
            raise ProviderError(
                "the egress allowlist names hosts that do not resolve to an address, so "
                f"no NetworkPolicy can permit them: {sorted(unresolved)}"
            )
        if forbidden:
            raise ProviderError(
                "the egress allowlist resolves into ranges this namespace denies, so no "
                f"NetworkPolicy may permit it: {sorted(forbidden)}"
            )
        return replace(plan, cidrs=tuple(dict.fromkeys(cidrs)))

    # ----- credentials (12) ---------------------------------------------

    def _credential_copy(self, spec: LaunchSpec) -> _CredentialCopy | None:
        adapter = self.harnesses.get(spec.harness)
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is None:
            return None
        if (
            not credential.required_for_launch
            and spec.harness not in self.config.credential_secrets
        ):
            return None
        secret_name = self.config.credential_secret_name(spec.harness)
        mode = credential.minimum_mode
        if self.config.credential_modes.get(spec.harness) is MountMode.RW_NARROW:
            mode = MountMode.RW_NARROW
        return _CredentialCopy(spec=credential, source_secret=secret_name, mode=mode)

    async def _credential_keys(self, attempt_id: str) -> list[str]:
        """Which auth files the per-attempt Secret actually holds, read back rather than
        assumed, so a restart between `prepare` and `launch` still projects the truth."""
        try:
            body = await self._call(
                self.client.get, "secrets", k8sspec.object_name("cred", attempt_id)
            )
        except KubernetesApiError:
            return []
        return [str(key) for key in (body.get("data") or {})]

    async def _seed_credential(self, spec: LaunchSpec, copy: _CredentialCopy) -> None:
        """26: per attempt, copy the harness Secret into `cred-<attempt>`.

        Only the named auth files, never the whole Secret: a harness Secret can hold
        state the adapter did not declare, and a copy is the narrowest thing that can
        authenticate (12). A required file that is missing refuses the launch rather
        than seeding a copy that cannot."""
        try:
            source = await self._call(self.client.get, "secrets", copy.source_secret)
        except KubernetesApiError as exc:
            raise HarnessRefusedError(
                f"refusing to launch: the credential Secret {copy.source_secret!r} for harness "
                f"{spec.harness!r} is not readable in {self.config.namespace} ({exc.status})"
            ) from exc
        data = source.get("data") or {}
        payload: dict[str, bytes] = {}
        for auth in copy.spec.auth_files:
            key = _secret_key(auth.name)
            raw = data.get(key)
            if raw is None:
                if auth.required:
                    raise HarnessRefusedError(
                        f"refusing to launch: the credential Secret {copy.source_secret!r} is "
                        f"missing its auth file {auth.name!r}"
                    )
                copy.seeded[auth.name] = None
                continue
            value = base64.b64decode(str(raw))
            payload[key] = value
            copy.seeded[auth.name] = hashlib.sha256(value).hexdigest()
        await self._create(
            "secrets",
            k8sspec.secret(
                name=k8sspec.object_name("cred", spec.attempt_id),
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                data=payload,
            ),
        )

    async def _sync_credential(
        self, h: Handle, spec: LaunchSpec, limits: Limits
    ) -> CredentialSync | None:
        """Read the rotated auth files back and write back only a valid, newer one (12).

        The removal is in a `finally` path: a cluster that keeps failing the read-back
        must not keep the copy for as long as it fails."""
        launched = self._launched.get(h.attempt_id)
        copy = launched.credential if launched is not None else self._credential_copy(spec)
        if copy is None:
            return None
        files: list[CredentialFileSync] = []
        try:
            if copy.writable:
                read = await self._read_files(
                    spec,
                    [f"{k8sspec.CREDENTIAL_LEAF}/{a.name}" for a in copy.spec.auth_files],
                    limits,
                    limit=CREDENTIAL_READ_LIMIT,
                )
                for auth in copy.spec.auth_files:
                    files.append(
                        await self._sync_file(
                            copy, auth, read.get(f"{k8sspec.CREDENTIAL_LEAF}/{auth.name}")
                        )
                    )
            else:
                files = [
                    CredentialFileSync(
                        auth.name, True, False, True, False, "read-only mount; nothing to sync"
                    )
                    for auth in copy.spec.auth_files
                ]
        finally:
            removed = await self._remove_credential(spec, copy)
        return CredentialSync(
            harness=copy.spec.harness,
            mount_mode=copy.mode.value,
            files=tuple(files),
            removed=removed,
            detail="" if copy.seeded else "seeded hashes unknown",
        )

    async def _sync_file(
        self, copy: _CredentialCopy, auth: AuthFile, data: bytes | None
    ) -> CredentialFileSync:
        if data is None:
            return CredentialFileSync(auth.name, False, False, False, False, "absent after the run")
        if data is _TRUNCATED:
            return CredentialFileSync(
                auth.name, True, True, False, False, "changed; larger than the read limit"
            )
        if data is _UNREADABLE:
            # 12: the sync is a recorded outcome, never an exception past the removal,
            # and "the read failed" is not "the file was absent".
            return CredentialFileSync(
                auth.name, False, False, False, False, "read failed: the exec stream ended early"
            )
        seeded = copy.seeded.get(auth.name)
        changed = seeded is None or hashlib.sha256(data).hexdigest() != seeded
        if not changed:
            return CredentialFileSync(auth.name, True, False, True, False, "unchanged")
        if not auth.sync_back:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; state, never written back"
            )
        from crucible.adapters.execution.docker import _issued_at  # noqa: PLC0415

        new_document: Any = None
        if auth.json:
            try:
                new_document = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                new_document = None
            if not isinstance(new_document, dict) or not all(
                key in new_document for key in auth.json_keys
            ):
                return CredentialFileSync(
                    auth.name, True, True, False, False, "changed; not the expected JSON shape"
                )
        if auth.issued_at is None:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; no issued-at field to order by"
            )
        newer = _issued_at(new_document, auth.issued_at)
        if newer is None:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; the copy carries no issued-at"
            )
        try:
            source = await self._call(self.client.get, "secrets", copy.source_secret)
        except KubernetesApiError as exc:
            return CredentialFileSync(
                auth.name, True, True, True, False, f"changed; source unreadable: {exc.status}"
            )
        raw = (source.get("data") or {}).get(_secret_key(auth.name))
        old_document: Any = None
        if raw:
            with contextlib.suppress(UnicodeDecodeError, ValueError):
                old_document = json.loads(base64.b64decode(str(raw)).decode("utf-8"))
        older = _issued_at(old_document, auth.issued_at)
        if older is not None and newer <= older:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; not newer than the source"
            )
        try:
            await self._call(
                self.client.patch,
                "secrets",
                copy.source_secret,
                {"data": {_secret_key(auth.name): base64.b64encode(data).decode("ascii")}},
            )
        except KubernetesApiError as exc:
            return CredentialFileSync(
                auth.name, True, True, True, False, f"changed; write back failed: {exc.status}"
            )
        return CredentialFileSync(
            auth.name, True, True, True, True, "changed; newer issued-at, written back"
        )

    async def _remove_credential(self, spec: LaunchSpec, copy: _CredentialCopy) -> bool:
        removed = await self._delete_credential_secret(spec.attempt_id)
        if copy.writable:
            try:
                await self._remove_from_claim(spec, [k8sspec.CREDENTIAL_LEAF])
            except Exception as exc:
                log.warning("credential copy removal failed", extra={"error": str(exc)})
                return False
        return removed

    async def _delete_credential_secret(self, attempt_id: str) -> bool:
        try:
            await self._call(self.client.delete, "secrets", k8sspec.object_name("cred", attempt_id))
        except KubernetesApiError as exc:
            log.warning("per-attempt secret removal failed", extra={"error": str(exc)})
            return False
        return True

    # ----- running a role -----------------------------------------------

    async def _run_role_job(
        self,
        spec: LaunchSpec,
        *,
        role: str,
        image: str,
        script: str,
        mounts: Sequence[Mount],
        volumes: Sequence[Mapping[str, Any]],
        limits: Limits,
        timeout: int,
        plan: EgressPlan,
        env: Mapping[str, str] | None = None,
    ) -> int:
        """Run one single-purpose Job to completion and delete it."""
        name = k8sspec.object_name(OBJECT_PREFIX.get(role, role), spec.attempt_id)
        policy_name: str | None = None
        with contextlib.suppress(KubernetesApiError):
            await self._call(self.client.delete, "jobs", name)
        try:
            policy_name = await self._apply_policy(spec, role, plan)
            body = k8sspec.job(
                name=name,
                namespace=self.config.namespace,
                object_labels=self._labels(spec, role),
                pod=k8sspec.pod_spec(
                    PodRequest(
                        role=role,
                        image=image,
                        command=["sh", "-c", script],
                        limits=limits,
                        env=dict(env or {}),
                        mounts=[*k8sspec.base_mounts(), *mounts],
                        volumes=[*k8sspec.base_volumes(limits), *volumes],
                        service_account=self.config.service_account,
                        image_pull_secret=self.config.image_pull_secret,
                    )
                ),
                active_deadline_seconds=timeout,
            )
            await self._create("jobs", body)
        except (KubernetesApiError, SpecError) as exc:
            log.warning("%s Job failed", role, extra={"error": str(exc)})
            self.last_error[role] = str(exc)
            return JOB_API_ERROR
        try:
            code = await self._await_job(name, timeout=timeout)
            if code is None:
                self.last_error[role] = f"the {role} Job did not finish within {timeout}s"
                return JOB_TIMED_OUT
            if code != 0:
                self.last_error[role] = await self._job_tail(name)
                log.warning("%s Job exited %s", role, code, extra={"tail": self.last_error[role]})
            return code
        finally:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "jobs", name)
            try:
                await self._await_job_pods_gone(name)
            finally:
                if policy_name:
                    with contextlib.suppress(KubernetesApiError):
                        await self._call(self.client.delete, "networkpolicies", policy_name)

    async def _run_verifier(
        self, spec: LaunchSpec, limits: Limits
    ) -> tuple[VerificationRun, ...] | None:
        checks = [
            (str(v.get("id")), str(v.get("command")))
            for v in spec.contract.get("required_verification", [])
            if str(v.get("kind", "command")) == "command"
        ]
        if not checks:
            return ()
        launched = self._launched.get(spec.attempt_id)
        code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_VERIFIER,
            image=launched.image_digest if launched else spec.image,
            script=scripts.verifier_script(checks),
            mounts=[
                Mount("ws", REPO_MOUNT, sub_path="output/tree"),
                Mount("ws", VERIFY_MOUNT, sub_path="verify"),
            ],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=limits,
            timeout=self.config.verifier_timeout_seconds,
            plan=self._egress_plan(spec, k8sspec.ROLE_VERIFIER),
        )
        # None means "the verifier could not be re-run": the caller marks every command
        # unverified rather than letting a gate read an exit nobody produced (11).
        return None if code in (JOB_TIMED_OUT, JOB_API_ERROR) else ()

    # ----- the reader Pod ------------------------------------------------

    @contextlib.asynccontextmanager
    async def _reader(self, spec: LaunchSpec, limits: Limits) -> Any:
        """A short-lived Pod with the workspace claim mounted read-only (26).

        The Crucible pods never mount a claim, so this is how everything a role wrote
        comes back. It carries the same pod shape as every other role, has no egress
        policy and therefore no network at all, and is deleted in the `finally`."""
        launched = self._launched.get(spec.attempt_id)
        image = launched.image_digest if launched else spec.image
        name = k8sspec.object_name(f"reader-{new_id().lower()[-8:]}", spec.attempt_id)[:63]
        body = k8sspec.bare_pod(
            name=name,
            namespace=self.config.namespace,
            object_labels=self._labels(spec, k8sspec.ROLE_READER),
            pod=k8sspec.pod_spec(
                PodRequest(
                    role=k8sspec.ROLE_READER,
                    image=image,
                    command=["sh", "-c", f"sleep {self.config.collector_timeout_seconds}"],
                    limits=limits,
                    mounts=[*k8sspec.base_mounts(), Mount("ws", WORK_MOUNT, read_only=True)],
                    volumes=[
                        *k8sspec.base_volumes(limits),
                        self._claim_volume(spec.attempt_id, read_only=True),
                    ],
                    service_account=self.config.service_account,
                    image_pull_secret=self.config.image_pull_secret,
                )
            ),
        )
        await self._create("pods", body)
        try:
            if not await self._await_running(name, timeout=self.config.launch_timeout_seconds):
                raise ProviderError(f"the reader Pod for {spec.attempt_id} never became ready")
            yield name
        finally:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "pods", name, grace_period_seconds=0)
            await self._await_pod_gone(name)

    async def _read_files(
        self,
        spec: LaunchSpec,
        paths: Sequence[str],
        limits: Limits,
        *,
        limit: int = CREDENTIAL_READ_LIMIT,
    ) -> dict[str, bytes]:
        """Read named files off the workspace claim through the reader Pod.

        The bytes come back on the exec stream and never through a Pod log: the kubelet
        writes logs to the node's disk, and a rotated credential there is exactly what
        12 forbids. Each file is checked for being a regular file and bounded before it
        is read, because a worker owns what it left at that path."""
        out: dict[str, bytes] = {}
        async with self._reader(spec, limits) as pod:
            for path in paths:
                result: ExecResult = await self._call(
                    self.client.pod_exec,
                    pod,
                    ["sh", "-c", _read_one_script(f"{WORK_MOUNT}/{path}", limit)],
                    container=k8sspec.CONTAINER_NAME,
                    limit=limit * 2,
                )
                if result.exit_code != 0:
                    # The stream ended before the command reported a status. "Could not
                    # read" is not "the file was absent", and 12 records the difference.
                    out[path] = _UNREADABLE
                    continue
                status, _, payload = result.stdout.partition(b"\n")
                token = status.decode("ascii", "replace").strip()
                if token == "ok":
                    with contextlib.suppress(ValueError):
                        out[path] = base64.b64decode(payload)
                elif token == "too-large":
                    out[path] = _TRUNCATED
        return out

    async def _read_workspace(self, spec: LaunchSpec, into: Path, limits: Limits) -> None:
        """The collected output and the verifier's logs, as a tar off the claim.

        `output/tree` is excluded: it is a git clone the verifier already ran against
        and nothing on the Crucible side reads it."""
        async with self._reader(spec, limits) as pod:
            result = await self._call(
                self.client.pod_exec,
                pod,
                ["sh", "-c", _OUTPUT_TAR_SCRIPT],
                container=k8sspec.CONTAINER_NAME,
                limit=OUTPUT_READ_LIMIT,
            )
        if result.exit_code != 0 or result.stderr:
            # A `None` exit is the API server never sending the error channel, which
            # means the stream ended early. Accepting it would let a partial tar through
            # `_extract`, which suppresses tar errors, and a report or a diff quietly
            # missing files is a wrong gate result rather than a visible failure.
            raise CollectionFailedError(
                "the reader Pod could not hand the collected output back "
                f"(exit {result.exit_code}, None means the stream ended early): "
                f"{result.stderr.decode('utf-8', 'replace')[:400]}"
            )
        if len(result.stdout) >= OUTPUT_READ_LIMIT:
            # 16: outputs Crucible could not read whole are an environment failure. A
            # partial extraction would give the gates a diff and a report quietly
            # missing files, which is worse than failing the attempt.
            raise CollectionFailedError(
                f"the collected output exceeded {OUTPUT_READ_LIMIT} bytes and was truncated"
            )
        if not result.stdout:
            return
        await asyncio.to_thread(_extract, result.stdout, into)

    async def _remove_from_claim(self, spec: LaunchSpec, leaves: Sequence[str]) -> None:
        """Remove leaves of the workspace claim through a Pod, never as this process.

        What a Pod made, a Pod removes: the claim belongs to the worker's uid and the
        Crucible process never mounts it (26)."""
        targets = " ".join(f'"{WORK_MOUNT}/{leaf}"' for leaf in leaves)
        code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_CLEANER,
            image=(
                self._launched.get(spec.attempt_id)
                or _Launched("", spec, spec.image, self._limits(spec))
            ).image_digest,
            script=f"rm -rf {targets} 2>/dev/null; exit 0\n",
            mounts=[Mount("ws", WORK_MOUNT)],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=self._limits(spec),
            timeout=120,
            plan=EgressPlan(),
        )
        if code != 0:
            # 12: a credential leaf that is still on the claim has not been removed,
            # whatever the caller would otherwise have recorded. The caller turns this
            # into `removed: False` and a log line rather than a silent success.
            raise ProviderError(
                f"the cleaner Job for {spec.attempt_id} exited {code}: "
                f"{self.last_error.get(k8sspec.ROLE_CLEANER, '')}"
            )

    # ----- waiting -------------------------------------------------------

    async def _await_job(self, name: str, *, timeout: int) -> int | None:
        """Wait for a Job's Pod to terminate and return the container's exit code."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pod = await self._pod_of(name)
            except KubernetesApiError:
                # A failed look is not an answer; ask again on the next poll.
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            if pod is not None:
                terminated = _terminated_state(pod.get("status") or {})
                if terminated is not None:
                    return int(terminated.get("exitCode", -1))
                phase = str((pod.get("status") or {}).get("phase", ""))
                if phase == "Failed":
                    return JOB_API_ERROR
            else:
                try:
                    job = await self._call(self.client.get, "jobs", name)
                except KubernetesApiError:
                    return JOB_API_ERROR
                if int((job.get("status") or {}).get("failed") or 0):
                    return JOB_API_ERROR
            await asyncio.sleep(self.config.poll_interval_seconds)
        return None

    async def _await_pod(self, name: str, *, timeout: int) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pod = await self._call(self.client.get, "pods", name)
            except KubernetesApiError:
                return None
            phase = str((pod.get("status") or {}).get("phase", ""))
            if phase in ("Succeeded", "Failed"):
                return phase
            await asyncio.sleep(self.config.poll_interval_seconds)
        return None

    async def _await_running(self, name: str, *, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pod = await self._call(self.client.get, "pods", name)
            except KubernetesApiError:
                return False
            if str((pod.get("status") or {}).get("phase", "")) == "Running":
                return True
            await asyncio.sleep(self.config.poll_interval_seconds)
        return False

    # ----- small API helpers ---------------------------------------------

    async def _create(self, kind: str, body: Mapping[str, Any]) -> None:
        try:
            await self._call(self.client.create, kind, body)
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise

    async def _pod_of(self, job_name: str) -> dict[str, Any] | None:
        """The Pod of a Job, or None when the namespace genuinely has none.

        It does not swallow a transport failure. A 503 from the API server, a reset
        connection or a timed-out list is "Crucible could not look", and answering None
        would make `observe` read that as "the Pod is gone", which is `lost` and
        terminal (16): a healthy worker would be failed and retried while the original
        Pod kept running. The caller decides; the ones that do not care suppress."""
        rows = await self._call(
            self.client.list_objects, "pods", label_selector=f"job-name={job_name}"
        )
        return rows[0] if rows else None

    async def _await_pod_gone(self, name: str, *, timeout: float = 15) -> None:
        """Wait for an asynchronous Pod deletion before recording workspace state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await self._call(self.client.get, "pods", name)
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(self.config.poll_interval_seconds)
        raise ProviderError(f"Pod {name!r} was still present after {timeout:g} seconds")

    async def _await_job_pods_gone(self, job_name: str, *, timeout: float = 15) -> None:
        """Wait for background Job propagation to remove its Pod."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = await self._call(
                self.client.list_objects, "pods", label_selector=f"job-name={job_name}"
            )
            if not rows:
                return
            await asyncio.sleep(self.config.poll_interval_seconds)
        raise ProviderError(
            f"Pods for Job {job_name!r} were still present after {timeout:g} seconds"
        )

    async def _job_tail(self, job_name: str, limit: int = 4000) -> str:
        try:
            pod = await self._pod_of(job_name)
        except KubernetesApiError:
            return ""
        if pod is None:
            return ""
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            frames = await self._call(
                self.client.pod_log, name, container=k8sspec.CONTAINER_NAME, timestamps=False
            )
        except KubernetesApiError:
            return ""
        return b"".join(f.payload for f in frames).decode("utf-8", "replace")[-limit:]

    async def _worker_tails(self, h: Handle) -> tuple[str, str]:
        """Kubernetes merges a Pod's two streams, so the whole tail is reported as
        stdout and the stderr tail is empty. Classification reads both (S5)."""
        body = await self._job_tail(h.ref, limit=self.config.log_tail_bytes)
        return body, ""

    async def _workspace_state(self, attempt_id: str) -> WorkspaceState:
        """11: nothing labelled for this attempt is still running once the collector and
        the verifier are gone."""
        try:
            rows = await self._call(
                self.client.list_objects,
                "pods",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ATTEMPT: attempt_id}),
            )
        except KubernetesApiError as exc:
            return WorkspaceState(checked=False, detail=str(exc))
        leftover = tuple(
            sorted(
                str((row.get("metadata") or {}).get("name", ""))
                for row in rows
                if str((row.get("status") or {}).get("phase", "")) in ("Pending", "Running")
                and str(((row.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_ROLE))
                != k8sspec.ROLE_WORKER
            )
        )
        return WorkspaceState(leftover=leftover)

    async def _delete_by_label(self, kinds: Sequence[str], *, attempt_id: str) -> None:
        for kind in kinds:
            try:
                rows = await self._call(
                    self.client.list_objects,
                    kind,
                    label_selector=k8sspec.selector(**{k8sspec.LABEL_ATTEMPT: attempt_id}),
                )
            except KubernetesApiError:
                continue
            for row in rows:
                name = str((row.get("metadata") or {}).get("name", ""))
                if name:
                    with contextlib.suppress(KubernetesApiError):
                        await self._call(self.client.delete, kind, name)

    async def _delete_attempt_objects(self, attempt_id: str) -> None:
        await self._delete_by_label(
            ("jobs", "pods", "networkpolicies", "secrets", "configmaps", "persistentvolumeclaims"),
            attempt_id=attempt_id,
        )

    async def _read_quota(self) -> int | None:
        """26: attempt capacity from the namespace's ResourceQuota."""
        rows = await self._call(self.client.list_objects, "resourcequotas")
        for row in rows:
            hard = (row.get("spec") or {}).get("hard") or {}
            if "count/jobs.batch" in hard:
                with contextlib.suppress(ValueError):
                    return int(str(hard["count/jobs.batch"])) // JOBS_PER_ATTEMPT
            if "pods" in hard:
                with contextlib.suppress(ValueError):
                    return int(str(hard["pods"]))
        return None


# ----- pure helpers -------------------------------------------------------


def _age_seconds(timestamp: str) -> float:
    """How long ago an object was created, from its RFC 3339 `creationTimestamp`."""
    if not timestamp:
        return 0.0
    try:
        return max(0.0, (datetime.now(UTC) - parse_rfc3339(timestamp)).total_seconds())
    except ValueError:
        return 0.0


def _resolve_host(host: str) -> list[str]:
    """Every IPv4 address a name resolves to, as /32 CIDRs. IPv6 is never emitted: no
    rule of this provider's policies carries a v6 block, so a v6 destination is denied
    by never appearing (26)."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({f"{info[4][0]}/32" for info in infos})


# Returned by `_read_files` for a path that was larger than the read limit. A distinct
# object so "the file was too big" is never confused with "the file held these bytes".
_TRUNCATED = b"\x00__crucible_truncated__"
# Returned for a path the reader Pod could not be asked about at all, because the exec
# stream ended before the command reported its status. Distinct from "absent", which is
# an answer, and from "truncated", which is a file that was there.
_UNREADABLE = b"\x00__crucible_unreadable__"


def _secret_key(name: str) -> str:
    """A Secret key for an auth file name. Keys may not contain a path separator."""
    return name.replace("/", "_")


def _bundle_key(relative: str) -> str:
    """A ConfigMap key for a bundle file. Keys are `[-._a-zA-Z0-9]+`, so the one
    separator a bundle path can carry becomes a double underscore and the volume's
    `items` maps it back to the real relative path."""
    return relative.replace("/", "__")


def _render_identity(
    spec: LaunchSpec, work_branch: str, harnesses: HarnessRegistry
) -> tuple[dict[str, str], dict[str, str], str]:
    """The identity bundle of 06, rendered exactly as the Docker provider renders it,
    then read back as ConfigMap data.

    The bundle is written to a temporary directory only so that one function writes it
    on both providers and the content hash means the same thing on both. The directory
    is removed before this returns; nothing of it is ever mounted."""
    scratch = Path(tempfile.mkdtemp(prefix="crucible-identity-"))
    try:
        adapter = harnesses.get(spec.harness)
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is not None and credential.templates:
            template_dir = scratch / k8sspec.TEMPLATE_PREFIX
            template_dir.mkdir(parents=True, exist_ok=True)
            for name, content in credential.templates.items():
                target = template_dir / name
                target.write_text(content, encoding="utf-8")
                os.chmod(target, 0o444)
        _, identity_sha = identity_bundle.write_bundle(
            scratch,
            contract=spec.contract,
            policy=spec.policy,
            external_id=spec.external_id,
            owner=spec.owner,
            work_branch=work_branch,
            network_mode=spec.network,
            report_schema=CompletionClaimV1.model_json_schema(),
        )
        data: dict[str, str] = {}
        paths: dict[str, str] = {}
        total = 0
        for path in sorted(p for p in scratch.rglob("*") if p.is_file()):
            relative = str(path.relative_to(scratch))
            content = path.read_text(encoding="utf-8")
            total += len(content.encode("utf-8"))
            key = _bundle_key(relative)
            data[key] = content
            paths[key] = relative
        if total > 1024 * 1024:
            # 08 and 26 name the ConfigMap size cap and an object-store projection above
            # it. Refusing is the honest answer until that projection exists: a
            # truncated identity bundle is a worker given the wrong contract.
            raise ProviderError(
                f"the identity bundle is {total} bytes, above the ConfigMap cap; "
                "26's projected-volume form is not implemented"
            )
        return data, paths, identity_sha
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _terminated_init(status: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first init container that terminated non-zero, with its name attached."""
    for entry in status.get("initContainerStatuses") or []:
        if not isinstance(entry, dict):
            continue
        terminated = (entry.get("state") or {}).get("terminated")
        if isinstance(terminated, dict) and int(terminated.get("exitCode", 0)) != 0:
            return {**terminated, "containerName": entry.get("name")}
    return None


def _terminated_state(status: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for entry in status.get("containerStatuses") or []:
        if not isinstance(entry, dict) or entry.get("name") != k8sspec.CONTAINER_NAME:
            continue
        terminated = (entry.get("state") or {}).get("terminated")
        if isinstance(terminated, dict):
            return terminated
    return None


def _merge_verifications(
    ran: tuple[VerificationRun, ...] | None,
    verify: Path,
    spec: LaunchSpec,
    timeout: int,
) -> tuple[VerificationRun, ...]:
    checks = [
        (str(v.get("id")), str(v.get("command")))
        for v in spec.contract.get("required_verification", [])
        if str(v.get("kind", "command")) == "command"
    ]
    if not checks:
        return ()
    runs = read_verifications(verify, spec, checks)
    if ran is None:
        detail = f"the verifier did not finish within {timeout}s"
        return tuple(
            run if run.ran and run.exit_code >= 0 else replace(run, ran=False, detail=detail)
            for run in runs
        )
    return runs


def _extract(raw: bytes, into: Path) -> None:
    """Extract the reader Pod's tar. `filter="data"` refuses an absolute path, a `..`
    component, a device, a symlink out of the tree, and anything else that is not a
    plain file or directory: the tar comes off a claim a worker wrote into."""
    into.mkdir(parents=True, exist_ok=True)
    with (
        contextlib.suppress(tarfile.TarError, EOFError),
        tarfile.open(fileobj=BytesIO(raw), mode="r|*") as tar,
    ):
        tar.extractall(into, filter="data")


def _read_probe(output: str) -> NamespaceProbe:
    """Parse the canary's answers. Anything but a definite refusal fails the probe.

    `done=1` is what says the script ran to the end; without it the output is a
    truncated log and nothing in it is a result."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key.startswith("crucible-canary."):
            fields[key[len("crucible-canary.") :].strip()] = value.strip()
    if "api" not in fields or fields.get("done") != "1":
        return NamespaceProbe(False, False, None, "the canary produced no result", checked=False)
    answer = fields["api"]
    enforced = answer == "unreachable"
    raw = fields.get("pids", "")
    pid_limit = int(raw) if raw.isdigit() else None
    # A canary that did not say is a canary that could not tell, never a pass.
    dns = fields.get("dns", "inconclusive")
    endpoint = fields.get("endpoint", "inconclusive")
    problems = []
    if answer == "reachable":
        problems.append("the canary reached the API server, so the CNI is not enforcing egress")
    elif not enforced:
        problems.append(
            "the canary could not tell whether it reached the API server "
            f"(tool {fields.get('tool', 'unknown')}, curl exit {fields.get('curl_exit', 'none')})"
        )
    if dns == "failed":
        problems.append(
            "DNS check failed: the canary could not resolve a cluster name under the "
            "worker egress rules, so a worker would have no DNS (check kubernetes.egress dns)"
        )
    elif dns != "resolved":
        problems.append(
            "DNS check inconclusive: the canary image has no getent or nslookup "
            f"(tool {fields.get('dns_tool', 'unknown')})"
        )
    if endpoint in ("unreachable", "unresolved"):
        problems.append(
            "local endpoint check failed: the canary could not "
            + ("resolve" if endpoint == "unresolved" else "connect to")
            + " the configured local endpoint under the worker egress rules "
            f"(curl exit {fields.get('endpoint_curl_exit', 'none')}; check "
            "kubernetes.egress local_endpoint)"
        )
    elif endpoint not in ("reachable", "none"):
        problems.append(
            "local endpoint check inconclusive: the canary could not tell whether it "
            f"connected (tool {fields.get('tool', 'unknown')}, curl exit "
            f"{fields.get('endpoint_curl_exit', 'none')})"
        )
    if pid_limit is None:
        problems.append("the node has no pod PID limit configured")
    return NamespaceProbe(
        passed=not problems,
        egress_enforced=enforced,
        pid_limit=pid_limit,
        detail="; ".join(problems) or "namespace ready",
        # An inconclusive answer is not a probe that ran: the status page should say so
        # rather than showing a namespace that merely failed.
        checked=answer != "inconclusive"
        and dns in ("resolved", "failed")
        and endpoint in ("reachable", "unreachable", "unresolved", "none"),
        dns_resolves={"resolved": True, "failed": False}.get(dns),
        local_endpoint_reachable={
            "reachable": True,
            "unreachable": False,
            "unresolved": False,
        }.get(endpoint),
    )


def _read_one_script(path: str, limit: int) -> str:
    """Read one file off the claim: a status word, then the bytes, base64 on one line.

    Never `cat` alone: a worker owns its credential copy and could have left a symlink,
    a directory or a gigabyte at that path (12)."""
    quoted = "'" + path.replace("'", "'\"'\"'") + "'"
    return (
        f"p={quoted}\n"
        'if [ -h "$p" ]; then echo not-regular; exit 0; fi\n'
        'if [ ! -f "$p" ]; then echo absent; exit 0; fi\n'
        'size=$(wc -c < "$p")\n'
        f'if [ "$size" -gt {limit} ]; then echo too-large; exit 0; fi\n'
        "echo ok\n"
        'base64 < "$p"\n'
    )


_OUTPUT_TAR_SCRIPT = f"""cd {WORK_MOUNT} || exit 1
set --
[ -d output ] && set -- "$@" output
[ -d verify ] && set -- "$@" verify
[ $# -eq 0 ] && exit 0
exec tar cf - --exclude=output/tree "$@"
"""

# The canary of 26: it must fail to reach the API server, and it reports the node's pod
# PID limit. Both answers go to its own log, which holds nothing secret.
#
# It also runs under the egress rules a worker gets and proves the two things a worker
# needs from them (crucible#91): a cluster name resolves, and the configured local
# endpoint takes a connection. A missing tool is `inconclusive` for the same reason as
# below, and so is anything curl says that is not a definite outcome.
#
# The reachability test fails *closed*. An earlier form used bash's `/dev/tcp` redirect,
# which is not a feature of `sh`: under dash or busybox the redirect simply fails, and
# the probe would have reported "unreachable" on a namespace with no egress enforcement
# at all, which is the one answer this gate exists to refuse to invent. So the test is
# curl, whose exit code says which happened, and anything that is not a definite refusal
# to connect is `inconclusive`, which does not pass the probe.
_CANARY_SCRIPT = """
host=${KUBERNETES_SERVICE_HOST:-kubernetes.default.svc}
port=${KUBERNETES_SERVICE_PORT:-443}
if ! command -v curl >/dev/null 2>&1; then
  echo "crucible-canary.api=inconclusive"
  echo "crucible-canary.tool=none"
else
  echo "crucible-canary.tool=curl"
  curl -sS -k -o /dev/null --max-time 5 "https://$host:$port/version" 2>/dev/null
  rc=$?
  case "$rc" in
    # Connected: 0 is a response, and 22/35/52/56/60 are TLS or HTTP outcomes that all
    # required a completed TCP connection to the API server.
    0|22|35|52|56|60) echo "crucible-canary.api=reachable" ;;
    # 7 is "failed to connect", 28 is "timed out": the CNI refused the packet.
    7|28) echo "crucible-canary.api=unreachable" ;;
    *) echo "crucible-canary.api=inconclusive" ;;
  esac
  echo "crucible-canary.curl_exit=$rc"
fi
name=${CRUCIBLE_CANARY_DNS_NAME:-kubernetes.default.svc}
bounded=""
command -v timeout >/dev/null 2>&1 && bounded="timeout 30"
if command -v getent >/dev/null 2>&1; then
  echo "crucible-canary.dns_tool=getent"
  if $bounded getent hosts "$name" >/dev/null 2>&1; then
    echo "crucible-canary.dns=resolved"
  else
    echo "crucible-canary.dns=failed"
  fi
elif command -v nslookup >/dev/null 2>&1; then
  echo "crucible-canary.dns_tool=nslookup"
  if $bounded nslookup "$name" >/dev/null 2>&1; then
    echo "crucible-canary.dns=resolved"
  else
    echo "crucible-canary.dns=failed"
  fi
else
  echo "crucible-canary.dns_tool=none"
  echo "crucible-canary.dns=inconclusive"
fi
url=${CRUCIBLE_CANARY_ENDPOINT_URL:-}
if [ -z "$url" ]; then
  echo "crucible-canary.endpoint=none"
elif ! command -v curl >/dev/null 2>&1; then
  echo "crucible-canary.endpoint=inconclusive"
else
  curl -sS -k -o /dev/null --max-time 10 "$url" 2>/dev/null
  rc=$?
  case "$rc" in
    0|22|35|52|56|60) echo "crucible-canary.endpoint=reachable" ;;
    6) echo "crucible-canary.endpoint=unresolved" ;;
    7|28) echo "crucible-canary.endpoint=unreachable" ;;
    *) echo "crucible-canary.endpoint=inconclusive" ;;
  esac
  echo "crucible-canary.endpoint_curl_exit=$rc"
fi
limit=$(cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max 2>/dev/null)
case "$limit" in
  ''|max) echo "crucible-canary.pids=none" ;;
  *) echo "crucible-canary.pids=$limit" ;;
esac
echo "crucible-canary.done=1"
"""


def _seed_script(spec: CredentialSpec) -> str:
    """The init container that makes a `rw-narrow` copy (12).

    It reads the per-attempt Secret's read-only projection and writes the same named
    files into the attempt's own claim, mode 0700 on the directory and 0600 on each
    file, owned by the worker's uid. The file list is written out rather than globbed:
    an auth file can sit in a subdirectory, and nothing but the adapter's declared files
    is ever copied."""
    names = " ".join("'" + a.name.replace("'", "'\"'\"'") + "'" for a in spec.auth_files)
    # An optional file the Secret did not carry is simply not projected, and the `-f`
    # test below skips it; nothing but the adapter's declared files is ever copied.
    return f"""set -eu
umask 077
src={k8sspec.CREDENTIAL_SOURCE_MOUNT}
dst=/crucible/credential
mkdir -p "$dst"
chmod 0700 "$dst"
for rel in {names}; do
  [ -f "$src/$rel" ] || continue
  mkdir -p "$dst/$(dirname "$rel")"
  cat < "$src/$rel" > "$dst/$rel"
  chmod 0600 "$dst/$rel"
done
"""


__all__ = [
    "PROVIDER_NAME",
    "CollectionFailedError",
    "HarnessRefusedError",
    "KubernetesConfig",
    "KubernetesProvider",
    "NamespaceProbe",
]
