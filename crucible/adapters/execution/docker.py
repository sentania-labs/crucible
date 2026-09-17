"""The Docker execution provider (08, 13).

Talks to the daemon only through the socket proxy. Creates workers, collectors, bundle
verifiers, and verifiers; nothing it creates ever receives the socket, the proxy
endpoint, the database, or another attempt's credential mount.

Four containers per attempt:

- the **worker**, from the allowlisted harness image, on the internal workers network
  with the egress proxy as its only way out;
- the **collector**, `--network none`, the checkout and the report directory read-only,
  which produces the diff, the changed paths, the head, the log, the bundle, a fresh
  tree, and a copy of the report;
- the **bundle verifier**, `--network none`, the collector's output read-only, which
  runs `git bundle verify` and nothing else;
- the **verifier**, network per policy, which re-runs each `required_verification`
  command from the collected tree. It is the one container that runs commands the
  repository defines, so it sees its own tree and its own log directory, never the
  collector's output.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal

import yaml

from crucible.adapters.execution import identity as identity_bundle
from crucible.adapters.execution import scripts, workspace
from crucible.adapters.execution.create_policy import (
    CreatePolicy,
    CreateRequestRefusedError,
    image_allowed,
)
from crucible.adapters.execution.create_policy import (
    check as check_create,
)
from crucible.adapters.execution.dockerapi import DockerApiError, DockerClient
from crucible.application.harnesses import REGISTRY, check_image_version, egress_allowlist
from crucible.contracts.completion_claim import CompletionClaimV1
from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
    BranchBundle,
    CleanupPolicy,
    CollectedArtifact,
    CollectedOutputs,
    Handle,
    IsolationLevel,
    LaunchSpec,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    ProviderCapabilities,
    ProviderError,
    VerificationRun,
    Workspace,
    WorkspaceState,
)

log = logging.getLogger("crucible.provider.docker")

PROVIDER_NAME = "docker"
# What a throwaway container's exit code is when Crucible never got one: the API call
# failed, or the wait timed out and the container was killed. Distinct from any code a
# container can exit with, so a caller can tell "it failed" from "it never finished".
THROWAWAY_API_ERROR = -1
THROWAWAY_TIMED_OUT = -2
LABEL_ATTEMPT = "crucible.attempt"
LABEL_TASK = "crucible.task"
LABEL_OWNER = "crucible.owner"
LABEL_ROLE = "crucible.role"
ROLE_WORKER = "worker"
ROLE_PREPARER = "preparer"
ROLE_CLEANER = "cleaner"
ROLE_COLLECTOR = "collector"
ROLE_BUNDLE = "bundle-verifier"
ROLE_VERIFIER = "verifier"

DEFAULT_IMAGE_ALLOWLIST: tuple[str, ...] = (
    "crucible-worker:*",
    "ghcr.io/sentania-labs/crucible-worker:*",
)


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """Everything the provider needs that is not on the launch spec."""

    endpoint: str
    artifact_root: str
    # How the artifact root appears to the daemon. Under the rootless arrangement the
    # Crucible service runs as uid 1000 and the artifact root is a named volume it
    # shares with every container it creates (S9 Test E), so the daemon needs no host
    # path at all. `bind` is the developer-mode shape, where the root is a host
    # directory the daemon can see.
    mount_kind: Literal["volume", "bind"] = "volume"
    artifact_volume: str = "crucible-artifacts"
    artifact_host_root: str | None = None
    credential_root: str | None = None
    credential_host_root: str | None = None
    workers_network: str = "crucible-workers"
    egress_proxy: str | None = None
    # What the egress proxy is actually configured to permit. An attempt whose
    # allowlist is not a subset of this is refused at launch rather than silently
    # running with less network than the policy promised (13).
    proxy_allowlist: tuple[str, ...] = ()
    no_proxy: str = "localhost,127.0.0.1"
    api_timeout_seconds: float = 30.0
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    report_size_cap_bytes: int = 10 * 1024 * 1024
    log_tail_bytes: int = 64 * 1024
    workspace_dir_mode: int = 0o755
    use_reference_cache: bool = True
    max_concurrency: int = 3
    extra_image_allowlist: tuple[str, ...] = field(default=())


class CollectionFailedError(ProviderError):
    """The collector could not produce the outputs. The attempt is an `environment`
    failure and nothing is collected from it (16)."""


@dataclass(frozen=True, slots=True)
class _ResolvedImage:
    """What the daemon says about a reference: the immutable form, and its labels.

    Only this is cached. Whether an attempt may run it is a question about that
    attempt's policy, and it is asked again every launch."""

    reference: str
    labels: dict[str, str]


@dataclass(slots=True)
class _Launched:
    container_id: str
    image_digest: str
    spec: LaunchSpec


def _mib(value: Any, default: int) -> int:
    """Parse `4GiB`, `512m`, or a plain byte count into bytes."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().replace("i", "")
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    if text and text[-1] == "b":
        text = text[:-1]
    if text and text[-1] in units:
        try:
            return int(float(text[:-1]) * units[text[-1]])
        except ValueError:
            return default
    try:
        return int(text)
    except ValueError:
        return default


class DockerProvider:
    """The provider of 08 against a rootless daemon behind the socket proxy (13, S9)."""

    name = PROVIDER_NAME

    def __init__(self, config: DockerConfig, client: DockerClient | None = None) -> None:
        self.config = config
        self.client = client or DockerClient(config.endpoint, timeout=config.api_timeout_seconds)
        self._launched: dict[str, _Launched] = {}
        self._images: dict[str, _ResolvedImage] = {}
        # The last failing output of each throwaway role, so an environment failure can
        # say what went wrong rather than only that something did.
        self.last_error: dict[str, str] = {}
        self._network_ready = False

    # ----- helpers -----------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _root(self, attempt_id: str) -> Path:
        return Path(self.config.artifact_root) / "workspaces" / attempt_id

    def _daemon_mount(
        self, attempt_id: str, leaf: str, target: str, *, read_only: bool
    ) -> dict[str, Any]:
        """One mount of a workspace subdirectory, in whichever shape the daemon needs.

        An empty leaf is the workspace root itself, which only the preparer gets."""
        relative = f"workspaces/{attempt_id}/{leaf}".rstrip("/")
        if self.config.mount_kind == "volume":
            return {
                "Type": "volume",
                "Source": self.config.artifact_volume,
                "Target": target,
                "ReadOnly": read_only,
                "VolumeOptions": {"Subpath": relative, "NoCopy": True},
            }
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return {
            "Type": "bind",
            "Source": f"{host_root.rstrip('/')}/{relative}",
            "Target": target,
            "ReadOnly": read_only,
            "BindOptions": {"Propagation": "rprivate"},
        }

    def _volume_mount(self, relative: str, target: str, *, read_only: bool) -> dict[str, Any]:
        """Mount a path under the artifact root that is not a workspace subdirectory."""
        if self.config.mount_kind == "volume":
            return {
                "Type": "volume",
                "Source": self.config.artifact_volume,
                "Target": target,
                "ReadOnly": read_only,
                "VolumeOptions": {"Subpath": relative, "NoCopy": True},
            }
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return {
            "Type": "bind",
            "Source": f"{host_root.rstrip('/')}/{relative}",
            "Target": target,
            "ReadOnly": read_only,
            "BindOptions": {"Propagation": "rprivate"},
        }

    def _create_policy(self, spec: LaunchSpec, *, resolved: str = "") -> CreatePolicy:
        allowlist = self._image_allowlist(spec)
        if resolved:
            allowlist.append(resolved)
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return CreatePolicy(
            image_allowlist=tuple(allowlist),
            artifact_root=host_root,
            credential_root=self.config.credential_host_root or self.config.credential_root,
            allowed_volumes=(self.config.artifact_volume,) if self.config.artifact_volume else (),
        )

    def _labels(self, spec: LaunchSpec, role: str) -> dict[str, str]:
        return {
            LABEL_ATTEMPT: spec.attempt_id,
            LABEL_TASK: spec.task_id,
            LABEL_OWNER: spec.owner,
            LABEL_ROLE: role,
        }

    def _hardened(self, spec: LaunchSpec, *, network: str, tmpfs_mb: int = 512) -> dict[str, Any]:
        resources = spec.policy.get("resources", {})
        memory = _mib(resources.get("memory"), 4 * 1024**3)
        cpus = float(resources.get("cpus") or 2)
        pids = int(resources.get("pids") or 512)
        return {
            # S5: without --init the harness is PID 1 and a SIGTERM is silently
            # dropped, so drain would always end in the SIGKILL after the grace.
            "Init": True,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Tmpfs": {
                "/tmp": f"rw,nosuid,nodev,size={tmpfs_mb}m",
                "/home/worker": f"rw,nosuid,nodev,size={tmpfs_mb}m,uid=1000,gid=1000,mode=0750",
            },
            "Memory": memory,
            "MemorySwap": memory,
            "NanoCpus": int(cpus * 1_000_000_000),
            "PidsLimit": pids,
            "NetworkMode": network,
            "RestartPolicy": {"Name": ""},
            "AutoRemove": False,
        }

    async def _ensure_network(self) -> None:
        if self._network_ready or self.config.workers_network in ("none", ""):
            return
        try:
            await self._call(self.client.inspect_network, self.config.workers_network)
        except DockerApiError as exc:
            if exc.status != 404:
                raise
            # internal: no default route, no reach to the control plane (13).
            await self._call(self.client.create_network, self.config.workers_network, internal=True)
        self._network_ready = True

    # ----- contract ----------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            isolation=IsolationLevel.CONTAINER,
            network_control=True,
            resource_limits=True,
            shared_disk=True,
            supports_harnesses=frozenset(REGISTRY),
            max_concurrency=self.config.max_concurrency,
        )

    async def prepare(self, spec: LaunchSpec) -> Workspace:
        repository = spec.contract.get("repository", {})
        url = spec.repository_url or str(repository.get("url", ""))
        if not url:
            raise ProviderError("the contract names no repository url")
        base_ref = str(repository.get("base_ref", "main"))
        work_branch = str(repository.get("work_branch") or f"crucible/{spec.external_id}")
        root = self._root(spec.attempt_id)
        await asyncio.to_thread(shutil.rmtree, root, True)
        paths = await asyncio.to_thread(self._make_dirs, root)
        resolved = await self._resolve_image(spec)

        local = self._local_origin(url)
        # The preparer gets the workspace itself, so git creates the checkout directory
        # and the container's uid owns it end to end (S9 Test E).
        mounts = [self._daemon_mount(spec.attempt_id, "", WORK_MOUNT, read_only=False)]
        network = "none"
        env: dict[str, str] = {}
        cache_name: str | None = None
        clone_url = url
        if local is not None:
            # A repository that already lives in the artifact root (the e2e origin) is
            # mounted read-only; nothing has to leave the daemon for it.
            mounts.append(self._volume_mount(local, scripts.ORIGIN_MOUNT, read_only=True))
            clone_url = scripts.ORIGIN_MOUNT
        else:
            if self.config.use_reference_cache:
                cache_name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                await asyncio.to_thread(self._ensure_cache_dir)
                mounts.append(self._volume_mount("cache", scripts.CACHE_MOUNT, read_only=False))
            network, env = self._network_and_env(spec)

        git_policy = spec.policy.get("git", {})
        exit_code = await self._run_throwaway(
            spec,
            role=ROLE_PREPARER,
            image=resolved,
            script=scripts.preparer_script(
                url=clone_url,
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
                shims=workspace.SHIM_NAMES,
                exclude_entries=workspace.EXCLUDE_ENTRIES,
                identity_mount=IDENTITY_MOUNT,
            ),
            mounts=mounts,
            network=network,
            timeout=self.config.collector_timeout_seconds,
            env=env,
        )
        if exit_code != 0:
            raise ProviderError(
                f"the preparer container could not build the checkout (exit {exit_code}): "
                f"{self.last_error.get(ROLE_PREPARER, '')}"
            )
        try:
            return await asyncio.to_thread(self._finish_prepare, spec, paths, work_branch)
        except workspace.WorkspaceError as exc:
            raise ProviderError(str(exc)) from exc

    def _ensure_cache_dir(self) -> None:
        cache = Path(self.config.artifact_root) / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        cache.chmod(self.config.workspace_dir_mode)

    def _make_dirs(self, root: Path) -> dict[str, Path]:
        """Everything but the checkout: the preparer's git creates that one."""
        paths = {
            "root": root,
            "identity": root / "identity",
            "report": root / "report",
            "output": root / "output",
            "verify": root / "verify",
        }
        for name, path in paths.items():
            path.mkdir(parents=True, exist_ok=True)
            if name != "identity":
                path.chmod(self.config.workspace_dir_mode)
        paths["repo"] = root / "repo"
        return paths

    def _local_origin(self, url: str) -> str | None:
        """The path inside the artifact root a local repository url names, if any."""
        candidate = url[len("file://") :] if url.startswith("file://") else url
        if not candidate.startswith("/"):
            return None
        root = Path(self.config.artifact_root).resolve()
        try:
            return str(Path(candidate).resolve().relative_to(root))
        except ValueError:
            return None

    def _finish_prepare(
        self, spec: LaunchSpec, paths: dict[str, Path], work_branch: str
    ) -> Workspace:
        output = paths["output"]
        head = (output / "prepared-head.txt").read_text(encoding="utf-8").strip()
        started_from = (output / "started-from.txt").read_text(encoding="utf-8").strip()
        if not head:
            raise workspace.WorkspaceError("the preparer produced no HEAD")
        _, identity_sha = identity_bundle.write_bundle(
            paths["identity"],
            contract=spec.contract,
            policy=spec.policy,
            external_id=spec.external_id,
            owner=spec.owner,
            work_branch=work_branch,
            network_mode=spec.network,
            report_schema=CompletionClaimV1.model_json_schema(),
        )
        # The preparer's own output files are not evidence; the collector rewrites the
        # directory after the run and a stale head would only confuse a reader.
        for leftover in ("prepared-head.txt", "started-from.txt"):
            (output / leftover).unlink(missing_ok=True)
        return Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=str(paths["repo"]),
            identity_path=str(paths["identity"]),
            report_path=str(paths["report"]),
            output_path=str(output),
            identity_sha256=identity_sha,
            work_branch=work_branch,
            started_from=started_from,
        )

    async def _resolve_image(self, spec: LaunchSpec) -> str:
        """Resolve the tag to something immutable, refusing anything the policy or the
        adapter's tested range does not allow (07, 13).

        The cache holds only the reference-to-digest mapping, which is a fact about the
        daemon. The allowlist and the harness-version check are facts about *this*
        attempt's policy and execution, so they run on every launch: the same image
        under a different policy has to be refused, and a cache hit must not be a way
        past that."""
        cached = self._images.get(spec.image)
        if cached is None:
            try:
                image = await self._call(self.client.inspect_image, spec.image)
            except DockerApiError as exc:
                raise ProviderError(
                    f"image {spec.image!r} is not available: {exc.message}"
                ) from exc
            labels = {
                str(k): str(v) for k, v in (image.get("Config", {}).get("Labels") or {}).items()
            }
            digests = [str(d) for d in (image.get("RepoDigests") or [])]
            cached = _ResolvedImage(
                reference=digests[0] if digests else str(image.get("Id", "")), labels=labels
            )
            self._images[spec.image] = cached
        self._check_image_allowed(spec, cached)
        return cached.reference

    def _check_image_allowed(self, spec: LaunchSpec, image: _ResolvedImage) -> None:
        """Every launch, cache hit or not (07, 13)."""
        if not image_allowed(spec.image, self._image_allowlist(spec)):
            raise ProviderError(f"image {spec.image!r} is outside the policy allowlist")
        check = check_image_version(spec.harness, image.labels)
        if not check.ok:
            raise ProviderError(f"refusing to launch: {check.detail}")

    def _image_allowlist(self, spec: LaunchSpec) -> list[str]:
        return [
            str(p)
            for p in (spec.policy.get("images", {}).get("allowlist") or DEFAULT_IMAGE_ALLOWLIST)
        ] + list(self.config.extra_image_allowlist)

    async def launch(self, ws: Workspace, spec: LaunchSpec) -> Handle:
        await self._ensure_network()
        resolved = await self._resolve_image(spec)
        network, env = self._network_and_env(spec)
        body = self._worker_body(ws, spec, resolved=resolved, network=network, env=env)
        try:
            check_create(body, self._create_policy(spec, resolved=resolved))
        except CreateRequestRefusedError as exc:
            raise ProviderError(f"create-request policy refused the worker: {exc}") from exc
        name = f"crucible-{spec.attempt_id}"
        try:
            container_id = await self._call(self.client.create_container, name, body)
            await self._call(self.client.start_container, container_id)
        except DockerApiError as exc:
            raise ProviderError(f"could not start the worker: {exc}") from exc
        self._launched[spec.attempt_id] = _Launched(container_id, resolved, spec)
        return Handle(
            provider=self.name,
            ref=container_id,
            attempt_id=spec.attempt_id,
            image_digest=resolved,
            name=name,
        )

    def _network_and_env(self, spec: LaunchSpec) -> tuple[str, dict[str, str]]:
        env: dict[str, str] = {
            "CRUCIBLE_ATTEMPT_ID": spec.attempt_id,
            "CRUCIBLE_TASK_EXTERNAL_ID": spec.external_id,
            "CRUCIBLE_IDENTITY_DIR": IDENTITY_MOUNT,
            "CRUCIBLE_REPORT_DIR": REPORT_MOUNT,
            "CRUCIBLE_REPO_DIR": REPO_MOUNT,
            "HOME": "/home/worker",
            **spec.env,
        }
        network_policy = str(spec.policy.get("network", {}).get("mode", "egress-proxy"))
        if spec.network == "none" or network_policy == "none":
            return "none", env
        wanted = egress_allowlist(
            spec.harness,
            [str(h) for h in (spec.policy.get("network", {}).get("egress_allowlist") or [])],
            [str(h) for h in (spec.contract.get("constraints", {}).get("egress_extra") or [])],
        )
        configured = set(self.config.proxy_allowlist)
        if configured and not set(wanted) <= configured:
            missing = sorted(set(wanted) - configured)
            raise ProviderError(
                "the egress proxy does not permit every host this attempt needs: "
                f"{missing}. Bring the proxy up with the policy's allowlist."
            )
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "HTTP_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "http_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        env["CRUCIBLE_EGRESS_ALLOWLIST"] = ",".join(wanted)
        return self.config.workers_network, env

    def _worker_body(
        self,
        ws: Workspace,
        spec: LaunchSpec,
        *,
        resolved: str,
        network: str,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        host_config = self._hardened(spec, network=network)
        host_config["Mounts"] = [
            self._daemon_mount(spec.attempt_id, "repo", REPO_MOUNT, read_only=False),
            self._daemon_mount(spec.attempt_id, "identity", IDENTITY_MOUNT, read_only=True),
            self._daemon_mount(spec.attempt_id, "report", REPORT_MOUNT, read_only=False),
            *self._credential_mounts(spec),
        ]
        harness = REGISTRY.get(spec.harness)
        command = list(spec.command) or list(harness.command if harness else ())
        return {
            "Image": resolved,
            "Cmd": command,
            "User": "1000:1000",
            "WorkingDir": REPO_MOUNT,
            "Env": [f"{k}={v}" for k, v in sorted(env.items())],
            "Labels": self._labels(spec, ROLE_WORKER),
            "Tty": False,
            "OpenStdin": False,
            "AttachStdin": False,
            "NetworkDisabled": False,
            "HostConfig": host_config,
        }

    def _credential_mounts(self, spec: LaunchSpec) -> list[dict[str, Any]]:
        """One credential directory, for this attempt's harness and no other (12).

        C3 runs the script harness, which has none. The shape is here so the isolation
        test can assert that a worker with its own credential mount still cannot see
        another harness's."""
        root = self.config.credential_root
        if not root:
            return []
        source = Path(root) / spec.harness
        if not source.exists():
            return []
        host_root = self.config.credential_host_root or root
        return [
            {
                "Type": "bind",
                "Source": f"{host_root.rstrip('/')}/{spec.harness}",
                "Target": f"/home/worker/.crucible-credential/{spec.harness}",
                "ReadOnly": True,
            }
        ]

    async def observe(self, h: Handle) -> Observation:
        try:
            data = await self._call(self.client.inspect_container, h.ref)
        except DockerApiError as exc:
            if exc.status == 404:
                return Observation(ObservationState.LOST, detail="the daemon has no such container")
            raise ProviderError(f"inspect failed: {exc}") from exc
        state = data.get("State", {})
        status = str(state.get("Status", ""))
        if status in ("created", "running", "restarting", "paused", "removing"):
            return Observation(ObservationState.RUNNING, detail=status)
        detail = status
        if state.get("OOMKilled"):
            # S5: 137 with OOMKilled is an environment failure, not a kill Crucible sent.
            detail = f"{status}:oom_killed"
        return Observation(
            ObservationState.EXITED, exit_code=int(state.get("ExitCode", -1)), detail=detail
        )

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        try:
            frames = await self._call(
                self.client.container_logs, h.ref, since=_since_param(since.timestamp)
            )
        except DockerApiError as exc:
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
        root = self._root(h.attempt_id)
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        repository = spec.contract.get("repository", {})
        work_branch = ws.work_branch or str(
            repository.get("work_branch") or f"crucible/{spec.external_id}"
        )
        collector_exit = await self._run_throwaway(
            spec,
            role=ROLE_COLLECTOR,
            script=scripts.collector_script(
                base_ref=str(repository.get("base_ref", "main")),
                work_branch=work_branch,
                size_cap_bytes=self.config.report_size_cap_bytes,
            ),
            mounts=[
                self._daemon_mount(spec.attempt_id, "repo", REPO_MOUNT, read_only=True),
                self._daemon_mount(spec.attempt_id, "report", REPORT_MOUNT, read_only=True),
                self._daemon_mount(spec.attempt_id, "output", OUTPUT_MOUNT, read_only=False),
            ],
            network="none",
            timeout=self.config.collector_timeout_seconds,
        )
        if collector_exit == THROWAWAY_TIMED_OUT:
            # 16: a provider that failed before or while producing the outputs is an
            # environment failure on the attempt. Raising here is what makes the
            # supervisor record one instead of collecting empty outputs every tick.
            raise CollectionFailedError(
                self.last_error.get(ROLE_COLLECTOR)
                or f"the collector did not finish within {self.config.collector_timeout_seconds}s"
            )
        if collector_exit == THROWAWAY_API_ERROR:
            raise CollectionFailedError(
                self.last_error.get(ROLE_COLLECTOR) or "the collector container could not be run"
            )
        bundle_ok = False
        if (
            collector_exit == 0
            and (output / "work_branch.bundle").exists()
            and (output / "tree").is_dir()
        ):
            bundle_ok = (
                await self._run_throwaway(
                    spec,
                    role=ROLE_BUNDLE,
                    script=scripts.BUNDLE_VERIFY_SCRIPT,
                    mounts=[
                        self._daemon_mount(spec.attempt_id, "output", OUTPUT_MOUNT, read_only=True)
                    ],
                    network="none",
                    timeout=120,
                )
                == 0
            )
        verifications = await self._run_verifier(spec)
        outputs = _read_outputs(
            output,
            root / "verify",
            spec=spec,
            bundle_verified=bundle_ok,
            collector_exit=collector_exit,
            verifications=verifications,
            tail_bytes=self.config.log_tail_bytes,
        )
        state = await self._workspace_state(spec.attempt_id, keep=h.ref)
        return CollectedOutputs(
            report=outputs.report,
            report_raw=outputs.report_raw,
            blocked_md=outputs.blocked_md,
            stdout_tail=outputs.stdout_tail,
            stderr_tail=outputs.stderr_tail,
            diff_paths=outputs.diff_paths,
            diff_text=outputs.diff_text,
            bundle=outputs.bundle,
            artifacts=outputs.artifacts,
            verifications=outputs.verifications,
            workspace_state=state,
            copy_rejections=outputs.copy_rejections,
        )

    async def _run_verifier(self, spec: LaunchSpec) -> tuple[VerificationRun, ...]:
        checks = [
            (str(v.get("id")), str(v.get("command")))
            for v in spec.contract.get("required_verification", [])
            if str(v.get("kind", "command")) == "command"
        ]
        if not checks:
            return ()
        tree = self._root(spec.attempt_id) / "output" / "tree"
        if not tree.exists():
            return tuple(
                VerificationRun(
                    id=check_id,
                    command=command,
                    expect_exit=0,
                    exit_code=-1,
                    log_tail="",
                    ran=False,
                    detail="the collector produced no tree to verify from",
                )
                for check_id, command in checks
            )
        network = "none" if spec.network == "none" else self.config.workers_network
        _, env = self._network_and_env(spec) if network != "none" else ("none", {})
        code = await self._run_throwaway(
            spec,
            role=ROLE_VERIFIER,
            script=scripts.verifier_script(checks),
            mounts=[
                self._daemon_mount(spec.attempt_id, "output/tree", REPO_MOUNT, read_only=False),
                self._daemon_mount(spec.attempt_id, "verify", VERIFY_MOUNT, read_only=False),
            ],
            network=network,
            timeout=self.config.verifier_timeout_seconds,
            env=env,
        )
        runs = _read_verifications(self._root(spec.attempt_id) / "verify", spec, checks)
        if code in (THROWAWAY_TIMED_OUT, THROWAWAY_API_ERROR):
            # 11: a command Crucible could not re-run has not been verified. The gate
            # fails with the reason rather than the attempt failing on an exception.
            detail = self.last_error.get(ROLE_VERIFIER) or (
                f"the verifier did not finish within {self.config.verifier_timeout_seconds}s"
            )
            runs = tuple(
                run if run.ran and run.exit_code >= 0 else replace(run, ran=False, detail=detail)
                for run in runs
            )
        return runs

    async def _run_throwaway(
        self,
        spec: LaunchSpec,
        *,
        role: str,
        script: str,
        mounts: list[dict[str, Any]],
        network: str,
        timeout: int,
        env: Mapping[str, str] | None = None,
        image: str | None = None,
    ) -> int:
        """Run one hardened, single-purpose container to completion and remove it."""
        launched = self._launched.get(spec.attempt_id)
        image = image or (launched.image_digest if launched else spec.image)
        host_config = self._hardened(spec, network=network)
        host_config["Mounts"] = mounts
        body: dict[str, Any] = {
            "Image": image,
            "Cmd": ["sh", "-c", script],
            "User": "1000:1000",
            "WorkingDir": "/tmp",
            "Env": [f"{k}={v}" for k, v in sorted((env or {}).items())],
            "Labels": self._labels(spec, role),
            "Tty": False,
            "HostConfig": host_config,
        }
        check_create(body, self._create_policy(spec, resolved=str(image)))
        name = f"crucible-{role}-{spec.attempt_id}"
        container_id = ""
        try:
            container_id = await self._call(self.client.create_container, name, body)
            await self._call(self.client.start_container, container_id)
            code = int(
                await self._call(self.client.wait_container, container_id, timeout=float(timeout))
            )
            if code != 0:
                # A throwaway container that failed is an environment failure, and its
                # own output is the only thing that says why.
                self.last_error[role] = await self._tail_of(container_id)
                log.warning(
                    "%s container exited %s", role, code, extra={"tail": self.last_error[role]}
                )
            return code
        except DockerApiError as exc:
            log.warning("%s container failed", role, extra={"error": str(exc)})
            self.last_error[role] = str(exc)
            return THROWAWAY_API_ERROR
        except (TimeoutError, OSError, HTTPException) as exc:
            # The wait outlived the transport. The container may still be running, so
            # it is killed before the finally clause removes it; nothing is left to be
            # waited on again, and the caller records a failure rather than retrying
            # this forever.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.last_error[role] = (
                f"the {role} container did not finish within {timeout}s ({detail})"
            )
            log.warning("%s container timed out", role, extra={"error": self.last_error[role]})
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.kill_container, container_id)
            return THROWAWAY_TIMED_OUT
        finally:
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.remove_container, container_id, force=True)

    async def _tail_of(self, container_id: str, limit: int = 4000) -> str:
        """What a failed throwaway container said, for the event that records it."""
        try:
            frames = await self._call(self.client.container_logs, container_id)
        except (DockerApiError, TimeoutError, OSError, HTTPException):
            return ""
        body = b"".join(frame.payload for frame in frames).decode("utf-8", "replace")
        return body[-limit:]

    async def _workspace_state(self, attempt_id: str, *, keep: str) -> WorkspaceState:
        try:
            rows = await self._call(
                self.client.list_containers, filters={"label": [f"{LABEL_ATTEMPT}={attempt_id}"]}
            )
        except DockerApiError as exc:
            return WorkspaceState(checked=False, detail=str(exc))
        leftover = tuple(
            sorted(
                str(row.get("Names", [row.get("Id")])[0]).lstrip("/")
                for row in rows
                if str(row.get("Id", "")) != keep
            )
        )
        return WorkspaceState(leftover=leftover)

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        signal = "SIGTERM" if mode == "drain" else "SIGKILL"
        try:
            await self._call(self.client.kill_container, h.ref, signal=signal)
        except DockerApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"terminate failed: {exc}") from exc

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None:
        """Only ever called for an attempt that recorded `logs_drained` (08)."""
        for row in await self._containers_for(ws.attempt_id):
            await self._call(self.client.remove_container, str(row["Id"]), force=True)
        root = self._root(ws.attempt_id)
        if policy is CleanupPolicy.KEEP:
            return
        leaves = (
            ("",)
            if policy is CleanupPolicy.DELETE
            # keep_diff_only: the checkout and the verifier's tree go, the collected
            # evidence (diff, bundle, report copy, verifier logs) stays.
            else ("repo", "output/tree")
        )
        for leaf in leaves:
            await asyncio.to_thread(shutil.rmtree, root / leaf if leaf else root, True)
        left = [leaf for leaf in leaves if (root / leaf if leaf else root).exists()]
        if left:
            # The tree belongs to the container's uid, which is not Crucible's in every
            # arrangement (S9 Test E), so what the daemon made, the daemon removes.
            await self._remove_through_daemon(ws, spec, left)

    async def _remove_through_daemon(
        self, ws: Workspace, spec: LaunchSpec | None, leaves: Sequence[str]
    ) -> None:
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            log.warning(
                "workspace left in place: no launch spec to remove it with",
                extra={"attempt_id": ws.attempt_id},
            )
            return
        targets = " ".join(
            f'"{WORK_MOUNT}/{leaf}"' if leaf else f'"{WORK_MOUNT}"/* "{WORK_MOUNT}"/.[!.]*'
            for leaf in leaves
        )
        await self._run_throwaway(
            spec,
            role=ROLE_CLEANER,
            script=f"rm -rf {targets} 2>/dev/null; exit 0\n",
            mounts=[self._daemon_mount(ws.attempt_id, "", WORK_MOUNT, read_only=False)],
            network="none",
            timeout=120,
        )

    async def _containers_for(self, attempt_id: str) -> list[dict[str, Any]]:
        try:
            return await self._call(  # type: ignore[no-any-return]
                self.client.list_containers, filters={"label": [f"{LABEL_ATTEMPT}={attempt_id}"]}
            )
        except DockerApiError:
            return []

    async def reconcile(self) -> list[Handle]:
        """Adopt by label (10). Only running workers are handles."""
        try:
            rows = await self._call(
                self.client.list_containers,
                all_states=False,
                filters={"label": [f"{LABEL_ROLE}={ROLE_WORKER}"]},
            )
        except DockerApiError as exc:
            raise ProviderError(f"reconcile failed: {exc}") from exc
        handles: list[Handle] = []
        for row in rows:
            labels = {str(k): str(v) for k, v in (row.get("Labels") or {}).items()}
            attempt_id = labels.get(LABEL_ATTEMPT)
            if not attempt_id:
                continue
            handles.append(
                Handle(
                    provider=self.name,
                    ref=str(row["Id"]),
                    attempt_id=attempt_id,
                    name=str((row.get("Names") or [""])[0]).lstrip("/"),
                )
            )
        return handles

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove containers and volumes labelled for attempts Crucible no longer
        tracks (16). Every removal the caller records as a RetentionAction."""
        live = set(keep)
        removed = 0
        try:
            rows = await self._call(self.client.list_containers, filters={"label": [LABEL_ATTEMPT]})
        except DockerApiError:
            return 0
        for row in rows:
            labels = {str(k): str(v) for k, v in (row.get("Labels") or {}).items()}
            attempt_id = labels.get(LABEL_ATTEMPT, "")
            if attempt_id and attempt_id not in live:
                await self._call(self.client.remove_container, str(row["Id"]), force=True)
                removed += 1
        return removed


# ----- reading what the collector wrote ---------------------------------


@dataclass(frozen=True, slots=True)
class _Outputs:
    report: dict[str, Any] | None
    report_raw: str | None
    blocked_md: str | None
    stdout_tail: str
    stderr_tail: str
    diff_paths: tuple[str, ...]
    diff_text: str | None
    bundle: BranchBundle | None
    artifacts: tuple[CollectedArtifact, ...]
    verifications: tuple[VerificationRun, ...]
    copy_rejections: tuple[dict[str, str], ...]


def _text(path: Path, limit: int = 8 * 1024 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _read_outputs(
    output: Path,
    verify: Path,
    *,
    spec: LaunchSpec,
    bundle_verified: bool,
    collector_exit: int,
    verifications: tuple[VerificationRun, ...],
    tail_bytes: int,
) -> _Outputs:
    report_dir = output / "report"
    report: dict[str, Any] | None = None
    report_raw: str | None = None
    report_file = report_dir / "report.yaml"
    if report_file.is_file():
        report_raw = _text(report_file)
        try:
            parsed = yaml.safe_load(report_raw)
            report = parsed if isinstance(parsed, dict) else None
        except yaml.YAMLError:
            report = None
    blocked = report_dir / "blocked.md"
    blocked_md = _text(blocked) if blocked.is_file() else None

    changed = tuple(p for p in _text(output / "changed.txt").splitlines() if p.strip())
    diff_text = _text(output / "diff.patch") if (output / "diff.patch").is_file() else None
    commit_paths = tuple(p for p in _text(output / "commit-paths.txt").splitlines() if p.strip())
    messages: list[str] = []
    for record in _text(output / "log.txt").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) >= 2 and parts[0]:
            messages.append(parts[1])
    head = _text(output / "head.txt").strip()
    commits_text = _text(output / "commits.txt").strip() or "0"
    repository = spec.contract.get("repository", {})
    bundle = None
    if head and collector_exit == 0:
        bundle = BranchBundle(
            head_sha=head,
            base_ref=str(repository.get("base_ref", "main")),
            work_branch=_text(output / "branch.txt").strip()
            or str(repository.get("work_branch", "")),
            commits=int(commits_text) if commits_text.isdigit() else 0,
            verified=bundle_verified,
            commit_paths=commit_paths,
            commit_messages=tuple(messages),
        )

    artifacts: list[CollectedArtifact] = []
    if report_dir.is_dir():
        for path in sorted(p for p in report_dir.rglob("*") if p.is_file()):
            name = f"report/{path.relative_to(report_dir)}"
            if path.name in ("report.yaml", "blocked.md"):
                continue
            artifacts.append(
                CollectedArtifact(
                    name=name,
                    type="run_evidence",
                    content=path.read_bytes()[: 4 * 1024 * 1024],
                    content_type="text/plain",
                )
            )
    for run in verifications:
        artifacts.append(
            CollectedArtifact(
                name=f"verify/{run.id}.log",
                type="verification_log",
                content=run.log_tail.encode("utf-8"),
                content_type="text/plain",
            )
        )
    rejections: list[dict[str, str]] = []
    for line in _text(output / "copy-rejections.tsv").splitlines():
        if "\t" in line:
            reason, path_text = line.split("\t", 1)
            rejections.append({"reason": reason, "path": path_text})
    return _Outputs(
        report=report,
        report_raw=report_raw,
        blocked_md=blocked_md,
        stdout_tail=_tail(output / "collector.ok", tail_bytes),
        stderr_tail=_tail(output / "bundle.log", tail_bytes),
        diff_paths=changed,
        diff_text=diff_text,
        bundle=bundle,
        artifacts=tuple(artifacts),
        verifications=verifications,
        copy_rejections=tuple(rejections),
    )


def _read_verifications(
    verify: Path, spec: LaunchSpec, checks: list[tuple[str, str]]
) -> tuple[VerificationRun, ...]:
    expected = {
        str(v.get("id")): int(v.get("expect_exit", 0))
        for v in spec.contract.get("required_verification", [])
    }
    runs: list[VerificationRun] = []
    for check_id, command in checks:
        safe = scripts.encode_check_id(check_id)
        exit_file = verify / f"{safe}.exit"
        log_file = verify / f"{safe}.log"
        if not exit_file.is_file():
            runs.append(
                VerificationRun(
                    id=check_id,
                    command=command,
                    expect_exit=expected.get(check_id, 0),
                    exit_code=-1,
                    log_tail=_tail(log_file, 32 * 1024),
                    ran=False,
                    detail="the verifier container recorded no exit for this command",
                )
            )
            continue
        raw = _text(exit_file).strip()
        runs.append(
            VerificationRun(
                id=check_id,
                command=command,
                expect_exit=expected.get(check_id, 0),
                exit_code=int(raw) if raw.lstrip("-").isdigit() else -1,
                log_tail=_tail(log_file, 32 * 1024),
            )
        )
    return tuple(runs)


# ----- log resume --------------------------------------------------------


def _since_param(timestamp: str | None) -> str | None:
    """Docker's `since` wants `<seconds>.<nanoseconds>`, not RFC 3339.

    The daemon splits the value on the dot and parses both halves as integers, so an
    RFC 3339 string is a 500. The stored offset stays RFC 3339 because that is what a
    reader and a comparison want; this is the wire form."""
    if not timestamp:
        return None
    try:
        moment = parse_rfc3339(timestamp)
    except ValueError:
        return None
    return f"{int(moment.timestamp())}.{moment.microsecond * 1000:09d}"


def _split(frame_stream: str, payload: bytes) -> list[tuple[str, datetime | None, str, bytes]]:
    out: list[tuple[str, datetime | None, str, bytes]] = []
    for raw in payload.split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        ts: datetime | None = None
        stamp, _, rest = line.partition(" ")
        try:
            ts = parse_rfc3339(stamp)
        except ValueError:
            rest = line
            stamp = ""
        out.append((frame_stream, ts, stamp, rest.encode("utf-8")))
    return out


def _chunks(frames: Sequence[Any], since: LogOffset) -> list[LogChunk]:
    """Demultiplexed frames into chunks, resuming strict-after the stored position.

    `--since` is inclusive (S8), so the batch always reopens at the boundary instant.
    The stored position is (timestamp, occurrence, sha256): the occurrence is which
    line at that instant was last stored, counting from 0, and it is what separates
    two identical lines logged in the same instant. Matching the last hash in the batch
    instead would silently swallow every repeat between the two.

    If the line at the stored position is not the stored hash, the stream is not the
    one the offset came from (rotated, truncated, a different container), and the pull
    falls back to strictly after the timestamp rather than guessing."""
    lines: list[tuple[str, datetime | None, str, bytes]] = []
    for frame in frames:
        lines.extend(_split(frame.stream, frame.payload))
    boundary: datetime | None = None
    if since.timestamp is not None:
        try:
            boundary = parse_rfc3339(since.timestamp)
        except ValueError:
            boundary = None
    carried = 0
    if boundary is not None:
        at_boundary = [index for index, entry in enumerate(lines) if entry[1] == boundary]
        position = since.occurrence
        matched = (
            position < len(at_boundary)
            and hashlib.sha256(lines[at_boundary[position]][3]).hexdigest() == since.line_sha256
        )
        if matched:
            lines = lines[at_boundary[position] + 1 :]
            carried = position + 1
        else:
            lines = [e for e in lines if e[1] is not None and e[1] > boundary]
    # Which line at its instant each kept line is. The boundary instant continues the
    # count from the offset rather than restarting it, because the lines before the
    # boundary were stored on an earlier pull.
    seen: dict[datetime | None, int] = {}
    if boundary is not None:
        seen[boundary] = carried
    occurrences: list[int] = []
    for entry in lines:
        position = seen.get(entry[1], 0)
        occurrences.append(position)
        seen[entry[1]] = position + 1

    chunks: list[LogChunk] = []
    buffer: list[bytes] = []
    stream = ""
    last: tuple[datetime | None, str, int] = (None, "", 0)
    count = 0
    for (line_stream, ts, _stamp, text), occurrence in zip(lines, occurrences, strict=True):
        if stream and line_stream != stream:
            chunks.append(_chunk(stream, buffer, last, count))
            buffer, count = [], 0
        stream = line_stream
        buffer.append(text)
        count += 1
        last = (ts, hashlib.sha256(text).hexdigest(), occurrence)
    if buffer:
        chunks.append(_chunk(stream, buffer, last, count))
    return chunks


def _chunk(
    stream: str, buffer: list[bytes], last: tuple[datetime | None, str, int], count: int
) -> LogChunk:
    content = b"\n".join(buffer) + b"\n"
    return LogChunk(
        stream="stderr" if stream == "stderr" else "stdout",
        content=content,
        ts=last[0],
        line_sha256=last[1],
        occurrence=last[2],
        lines=count,
    )
