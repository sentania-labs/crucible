"""Credential administration (12, 25): validate, the bounded probe, rotate, remove.

Nothing here reads a credential value into a record. Validation is a shape check of
the named files (they exist, they parse, they carry the expected keys); the probe is a
run in the hardened image that records exit facts and whether the files changed; rotate
is an atomic rename with the previous directory retained and then shredded; remove
shreds. Every step is an event with the principal and the reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
)
from crucible.application.errors import ConflictError, ContractValidationError, NotFoundError
from crucible.application.harnesses import (
    credential_state,
    effective_mount_mode,
    record_credential_observation,
    record_launch_outcome,
    set_harness_enabled,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    ExecutionProvider,
    ProbeRequest,
    ProbeResult,
)
from crucible.ports.harness import (
    CredentialSource,
    CredentialSpec,
    ExitInfo,
    HarnessAdapter,
    LaunchContext,
    MountMode,
)
from crucible.ports.repository import UnitOfWork

PROBE_PROMPT = "Reply with exactly the word OK and nothing else. Do not read or change any file."
RETIRED_MARK = ".retired-"
INCOMING_MARK = ".incoming-"
SHRED_CHUNK = 1024 * 1024


class CredentialAdminError(ConflictError):
    slug = "credential-admin"
    title = "Credential operation refused"


@dataclass(frozen=True, slots=True)
class ShapeCheck:
    ok: bool
    files: tuple[dict[str, Any], ...]
    problems: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files": [dict(f) for f in self.files],
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """What the probe records (25): exit class, harness version, image digest, whether
    the auth files changed, and the duration. Nothing else."""

    harness: str
    exit_class: str
    exit_code: int | None
    harness_version: str | None
    image: str
    image_digest: str
    auth_files_changed: bool
    mount_mode: str
    duration_seconds: float
    files: tuple[dict[str, Any], ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "exit_class": self.exit_class,
            "exit_code": self.exit_code,
            "harness_version": self.harness_version,
            "image": self.image,
            "image_digest": self.image_digest,
            "auth_files_changed": self.auth_files_changed,
            "mount_mode": self.mount_mode,
            "duration_seconds": self.duration_seconds,
            "files": [dict(f) for f in self.files],
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CredentialReport:
    harness: str
    state: dict[str, Any]
    shape: ShapeCheck | None = None
    probe: ProbeRecord | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"harness": self.harness, "credential": self.state}
        if self.shape is not None:
            out["shape"] = self.shape.as_dict()
        if self.probe is not None:
            out["probe"] = self.probe.as_dict()
        out.update(self.extra)
        return out


# ----- lookups -------------------------------------------------------------


def adapter_for(ctx: AdminContext, harness: str) -> HarnessAdapter:
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    return adapter


def spec_for(ctx: AdminContext, harness: str) -> CredentialSpec:
    credential = adapter_for(ctx, harness).credential_spec()
    if credential is None:
        raise CredentialAdminError(f"harness {harness!r} needs no credential")
    return credential


def source_for(ctx: AdminContext, harness: str) -> CredentialSource:
    source = ctx.credential_sources.get(harness)
    if source is None or not source.path:
        raise CredentialAdminError(
            f"no credential directory is configured for harness {harness!r} "
            f"(credentials.{harness}.path)"
        )
    return source


def state_view(ctx: AdminContext, uow: UnitOfWork, harness: str) -> dict[str, Any]:
    adapter = adapter_for(ctx, harness)
    state = uow.harnesses.get(harness)
    view = credential_state(
        adapter.credential_spec(), ctx.credential_sources.get(harness), state
    ).as_dict()
    view.update(
        {
            "session_compatibility": state.session_compatibility if state else "unverified",
            "refresh_requires_rw": state.refresh_requires_rw if state else None,
            "mount_mode_observed": state.mount_mode_observed if state else None,
            "last_validated_at": _iso(state.last_validated_at) if state else None,
            "last_auth_failure_at": _iso(state.last_auth_failure_at) if state else None,
            "last_launch_at": _iso(state.last_launch_at) if state else None,
            "last_launch_outcome": state.last_launch_outcome if state else None,
        }
    )
    return view


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


# ----- shape check ---------------------------------------------------------


def check_shape(spec: CredentialSpec, path: str) -> ShapeCheck:
    """25 step 4: the named auth files exist, parse, and carry the expected fields.
    Nothing is printed; the record says which file failed and why, never what it held."""
    files: list[dict[str, Any]] = []
    problems: list[str] = []
    for auth in spec.auth_files:
        target = spec.source_path(path, auth.name)
        entry: dict[str, Any] = {"name": auth.name, "required": auth.required}
        try:
            stat = target.stat()
        except OSError:
            entry["present"] = False
            if auth.required:
                problems.append(f"{auth.name}: missing")
            files.append(entry)
            continue
        entry["present"] = True
        entry["size"] = stat.st_size
        entry["mode"] = oct(stat.st_mode & 0o777)
        if stat.st_size == 0:
            problems.append(f"{auth.name}: empty")
        if auth.json:
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                problems.append(f"{auth.name}: not JSON")
                entry["parses"] = False
                files.append(entry)
                continue
            entry["parses"] = isinstance(document, dict)
            if not isinstance(document, dict):
                problems.append(f"{auth.name}: not a JSON object")
            else:
                missing = [k for k in auth.json_keys if k not in document]
                if missing:
                    problems.append(f"{auth.name}: missing keys {missing}")
        files.append(entry)
    return ShapeCheck(ok=not problems, files=tuple(files), problems=tuple(problems))


async def validate(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> CredentialReport:
    """25: shape check of the named auth files, then the bounded probe; returns state
    and timestamps only. The shape check alone marks `invalid`; the probe decides
    `validated`."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials validate {harness}"
    )
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    before = state_view(ctx, uow, harness)
    shape = check_shape(spec, source.path)
    probe: ProbeRecord | None = None
    if shape.ok:
        probe = await _probe_async(ctx, uow, harness=harness, principal=principal, reason=reason)
    now = ctx.clock.now()
    state = uow.harnesses.get(harness)
    validated = (
        shape.ok
        and probe is not None
        and probe.exit_class
        in (
            ExitClass.COMPLETED.value,
            ExitClass.COMPLETED_WITHOUT_REPORT.value,
        )
    )
    if state is not None:
        if validated:
            state.last_validated_at = now
        else:
            # 25: an unsuccessful validation yields `invalid`, whether the shape check or
            # the probe is what failed. Without this a probe that timed out or crashed
            # left a previously validated credential reading `validated`, which is the
            # one thing the operator would act on.
            state.last_auth_failure_at = now
        state.updated_at = now
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_VALIDATED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=harness,
        shape=shape.as_dict(),
        validated=validated,
    )
    return CredentialReport(
        harness, after, shape=shape, probe=probe, extra={"validated": validated}
    )


# ----- the bounded probe ---------------------------------------------------


def _probe_provider(ctx: AdminContext) -> ExecutionProvider:
    docker = ctx.providers.get("docker")
    if docker is not None:
        return docker
    if ctx.providers:
        return next(iter(ctx.providers.values()))
    raise CredentialAdminError("no execution provider is configured for the probe")


async def probe_image(
    ctx: AdminContext, uow: UnitOfWork, provider: ExecutionProvider, harness: str
) -> str:
    """The image the probe runs: the promoted default for the harness, else the one
    labelled image the provider has for it, else a refusal naming the ambiguity (13)."""
    promoted = [
        p for p in uow.image_promotions.list_all() if p.harness == harness and p.state == "default"
    ]
    if promoted:
        return promoted[0].reference
    if provider.name == "fake":
        # The fake provider runs behaviours, not images (08).
        return "crucible-worker:fake-probe"
    images = [i for i in await provider.list_images() if i.harness == harness]
    references = sorted({i.reference for i in images})
    if len(references) == 1:
        return references[0]
    if not references:
        raise CredentialAdminError(f"the provider has no image labelled for harness {harness!r}")
    raise CredentialAdminError(
        f"{len(references)} images are labelled for {harness!r} and none is promoted; "
        "promote one (images promote) so the probe knows which to run"
    )


async def _probe_async(
    ctx: AdminContext, uow: UnitOfWork, *, harness: str, principal: str, reason: str
) -> ProbeRecord:
    adapter = adapter_for(ctx, harness)
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    provider = _probe_provider(ctx)
    image = await probe_image(ctx, uow, provider, harness)
    mode = effective_mount_mode(spec, source)
    launch = adapter.build_launch(
        LaunchContext(
            attempt_id="probe",
            model=_probe_model(uow, adapter, harness),
            effort=None,
            timeout_seconds=ctx.probe_timeout_seconds,
            identity_mount=IDENTITY_MOUNT,
            report_mount=REPORT_MOUNT,
            repo_mount=REPO_MOUNT,
            credential_mounted=True,
        )
    )
    result: ProbeResult = await provider.probe_credential(
        ProbeRequest(
            harness=harness,
            image=image,
            argv=tuple(launch.argv),
            env=dict(launch.env),
            env_from_files=dict(launch.env_from_files),
            stdin_files=tuple(launch.stdin_files),
            stdin_text=PROBE_PROMPT if launch.stdin_text else "",
            identity_text=f"# Probe\n\n{PROBE_PROMPT}\n",
            timeout_seconds=ctx.probe_timeout_seconds,
            policy={
                "resources": {"cpus": 2, "memory": "3GiB", "pids": 1024, "tmpfs_total": "1GiB"},
                "network": {"mode": "egress-proxy", "egress_allowlist": []},
                "images": {"allowlist": [image]},
            },
        )
    )
    exit_class = adapter.classify_exit(
        ExitInfo(
            exit_code=result.exit_code,
            report_present=False,
            timed_out=result.timed_out,
            oom_killed=result.oom_killed,
        ),
        result.stdout_tail,
        result.stderr_tail,
    )
    # A probe asks for no report: exit 0 without one is the probe's success.
    if exit_class is ExitClass.COMPLETED_WITHOUT_REPORT:
        exit_class = ExitClass.COMPLETED
    sync = result.credential_sync
    changed = bool(sync and sync.changed)
    record = ProbeRecord(
        harness=harness,
        exit_class=exit_class.value,
        exit_code=result.exit_code,
        harness_version=result.harness_version,
        image=image,
        image_digest=result.image_digest,
        auth_files_changed=changed,
        mount_mode=mode.value,
        duration_seconds=round(result.duration_seconds, 1),
        files=tuple(f.as_dict() for f in sync.files) if sync else (),
        detail=result.detail,
    )
    now = ctx.clock.now()
    record_launch_outcome(
        uow,
        ctx.clock,
        name=harness,
        outcome=f"probe:{exit_class.value}",
        at=now,
        auth_failure=exit_class is ExitClass.AUTH_FAILURE,
    )
    record_credential_observation(
        uow, ctx.clock, name=harness, mount_mode=MountMode(mode.value), changed=changed, at=now
    )
    record_event_probe(uow, ctx, principal, record, reason)
    return record


def record_event_probe(
    uow: UnitOfWork, ctx: AdminContext, principal: str, record: ProbeRecord, reason: str
) -> None:
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_PROBED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        **record.as_dict(),
    )


def _routing_documents(uow: UnitOfWork) -> list[dict[str, Any]]:
    """The routing policy the newest `default-software` names first, then the seeded
    `default-routing` versions, newest first."""
    documents: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    versions = list(uow.policies.list_versions("default-software"))
    if versions:
        newest = max(versions, key=lambda p: p.version)
        ref = newest.document.get("routing", {}).get("policy", {})
        if isinstance(ref, dict) and ref.get("name") and ref.get("version") is not None:
            record = uow.routing_policies.get(str(ref["name"]), int(ref["version"]))
            if record is not None:
                documents.append(record.document)
                seen.add((str(ref["name"]), int(ref["version"])))
    for version in (2, 1):
        if ("default-routing", version) in seen:
            continue
        record = uow.routing_policies.get("default-routing", version)
        if record is not None:
            documents.append(record.document)
    return documents


def _probe_model(uow: UnitOfWork, adapter: HarnessAdapter, harness: str) -> str:
    """The cheapest enabled model the routing policy names for the harness, so a probe
    never runs on a frontier model. A harness without a model flag (the script harness)
    needs none. Otherwise a routing policy without an enabled model for the harness is a
    refusal: the CLIs reject an unknown model name, so guessing one would only produce
    a crash that says nothing about the credential (AGY did exactly that, C5b live run).
    """
    if not adapter.capabilities().model_flag:
        return "none"
    order = {"small": 0, "mid": 1, "frontier": 2}
    for document in _routing_documents(uow):
        best: tuple[int, str] | None = None
        for model in document.get("models", []):
            if model.get("harness") == harness and model.get("enabled"):
                rank = order.get(str(model.get("capability")), 3)
                if best is None or rank < best[0]:
                    best = (rank, str(model["id"]))
        if best is not None:
            return best[1]
    raise CredentialAdminError(
        f"no routing policy names an enabled model for harness {harness!r}; the probe "
        "needs one (upload a routing policy or enable a model in it)"
    )


async def probe(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> CredentialReport:
    """25: the bounded probe on its own, 120 s, records exit class, harness version,
    image digest and whether the auth files changed; removes everything."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials probe {harness}"
    )
    spec_for(ctx, harness)
    source_for(ctx, harness)
    record = await _probe_async(ctx, uow, harness=harness, principal=principal, reason=reason)
    return CredentialReport(harness, state_view(ctx, uow, harness), probe=record)


# ----- rotate, remove, shred -------------------------------------------------


def shred_file(path: Path) -> int:
    """Overwrite a regular file with zeros in place, fsync, unlink. Returns bytes."""
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        remaining = size
        while remaining > 0:
            chunk = min(SHRED_CHUNK, remaining)
            handle.write(b"\0" * chunk)
            remaining -= chunk
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()
    return size


class ShredIncompleteError(CredentialAdminError):
    """A shred that did not remove everything. It is never a success: the operator asked
    for a credential to be gone and part of it is still on disk."""

    slug = "shred-incomplete"
    title = "The credential was not fully shredded"


def _remove_entry(path: Path) -> int | None:
    """One entry, whatever it is. A symlink, socket, fifo or device node holds no bytes
    of its own, so it is unlinked and not counted as a file shredded; only a regular file
    is overwritten first."""
    if path.is_symlink() or not path.is_file():
        path.unlink()
        return None
    return shred_file(path)


def _shred_pass(root: Path) -> tuple[int, int, list[str]]:
    """One bottom-up pass. Nothing here aborts the walk: an entry that cannot be removed
    is recorded and the rest of the tree is still shredded, because stopping at the first
    failure is what left an auth file in place behind a directory that would not go."""
    files = 0
    total = 0
    failed: list[str] = []
    walker = os.walk(
        root, topdown=False, onerror=lambda exc: failed.append(str(getattr(exc, "filename", root)))
    )
    for dirpath, dirnames, filenames in walker:
        here = Path(dirpath)
        for name in filenames:
            try:
                size = _remove_entry(here / name)
            except OSError:
                failed.append(str(here / name))
                continue
            if size is not None:
                total += size
                files += 1
        for name in dirnames:
            child = here / name
            try:
                if child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
            except OSError:
                failed.append(str(child))
    return files, total, failed


def shred_tree(root: Path, *, keep_root: bool) -> dict[str, int]:
    """Shred every file under root and remove every subdirectory; the root stays when
    asked (the configured path is what the next login points at).

    Two passes, because a file the harness CLI writes between the listing and the parent
    directory's removal is a benign race the second pass absorbs. Anything still there
    after that is a refusal, not a rounded-down success: the previous walk was ordered
    only by depth and stopped at the first directory that would not go, which left files
    queued behind it on disk while the caller was told the shred had worked.
    """
    files = 0
    total = 0
    remaining: list[str] = []
    for _ in range(2):
        pass_files, pass_bytes, remaining = _shred_pass(root)
        files += pass_files
        total += pass_bytes
        if not remaining:
            break
    if not remaining and not keep_root:
        try:
            root.rmdir()
        except OSError:
            remaining = [str(root)]
    if remaining:
        raise ShredIncompleteError(
            f"{len(remaining)} entries under {root} could not be removed, so the "
            f"credential is still partly on disk: {sorted(set(remaining))[:5]}"
        )
    return {"files": files, "bytes": total}


def rotate(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    new_path: str,
    reason: str | None,
) -> CredentialReport:
    """25: a new directory is prepared and validated first; the swap is an atomic
    rename; the previous directory is retained beside it for `credential_retention_hours`
    and then shredded; every step an event.

    `new_path` is an operator-typed path that Crucible does not own, so rotate copies it
    and leaves it exactly as it found it. The only thing rotate ever destroys is inside
    the configured credential root, and even that goes on the retention schedule rather
    than now. Disposing of the source is the operator's to do, and the response and the
    event both say it was left."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials rotate {harness}"
    )
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    incoming = Path(new_path)
    if not incoming.is_dir():
        raise ContractValidationError(
            "the new credential directory does not exist",
            errors=[{"path": "new_path", "message": f"{new_path} is not a directory"}],
        )
    shape = check_shape(spec, str(incoming))
    if not shape.ok:
        raise ContractValidationError(
            "the new credential directory failed the shape check",
            errors=[{"path": "new_path", "message": p} for p in shape.problems],
        )
    current = Path(source.path)
    before = state_view(ctx, uow, harness)
    stamp = ctx.clock.now().strftime("%Y%m%dT%H%M%SZ")
    staged = current.with_name(current.name + INCOMING_MARK + stamp)
    if incoming.resolve() == current.resolve():
        raise CredentialAdminError("the new directory is the configured directory itself")
    # Stage beside the target so the final step is one rename on one filesystem.
    shutil.copytree(incoming, staged, symlinks=False)
    _tighten(staged)
    retired = current.with_name(current.name + RETIRED_MARK + stamp)
    _swap(ctx, current, retired, staged, principal=principal, harness=harness)
    state = uow.harnesses.get(harness)
    if state is not None:
        state.last_validated_at = None
        state.last_auth_failure_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_ROTATED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=harness,
        retained_as=retired.name if retired.exists() else None,
        retention_hours=ctx.credential_retention_hours,
        source_kept=str(incoming),
        shape=shape.as_dict(),
    )
    return CredentialReport(
        harness,
        after,
        shape=shape,
        extra={
            "retained_as": retired.name if retired.exists() else None,
            # The caller's directory is untouched and still holds the credential: the
            # operator disposes of it, because Crucible does not own that path.
            "source_kept": str(incoming),
        },
    )


def _swap(
    ctx: AdminContext,
    current: Path,
    retired: Path,
    staged: Path,
    *,
    principal: str,
    harness: str,
) -> None:
    """The two renames as one step. If the second fails the configured path would be
    gone, so the first is undone and the staged copy shredded: a failed rotate leaves
    exactly what it found. The failure is recorded through a unit of work of its own,
    because the caller's transaction is about to roll back with the exception."""
    moved = False
    if current.exists():
        os.rename(current, retired)
        moved = True
    try:
        os.rename(staged, current)
    except OSError as exc:
        if moved and not current.exists():
            os.rename(retired, current)
        with contextlib.suppress(OSError):
            if staged.is_dir():
                shred_tree(staged, keep_root=False)
        record_refusal(
            ctx,
            principal=principal,
            operation=f"credentials rotate {harness}",
            detail=f"the swap failed and was rolled back: {type(exc).__name__}",
        )
        raise CredentialAdminError(
            f"the credential swap failed ({type(exc).__name__}) and was rolled back; "
            "the configured directory is unchanged"
        ) from exc


def _tighten(root: Path) -> None:
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            os.chmod(path, 0o600)


def remove(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> CredentialReport:
    """25: the harness becomes `absent`; the files are shredded; the harness is
    disabled with the reason so a launch is refused cleanly rather than failing auth."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials remove {harness}"
    )
    spec_for(ctx, harness)
    source = source_for(ctx, harness)
    before = state_view(ctx, uow, harness)
    root = Path(source.path)
    shredded = shred_tree(root, keep_root=True) if root.is_dir() else {"files": 0, "bytes": 0}
    set_harness_enabled(
        uow,
        ctx.clock,
        principal_name=principal,
        name=harness,
        enabled=False,
        reason=f"credential removed: {reason}",
    )
    state = uow.harnesses.get(harness)
    if state is not None:
        state.last_validated_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_REMOVED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=harness,
        shredded=shredded,
    )
    return CredentialReport(harness, after, extra={"shredded": shredded})


def sweep_retired(ctx: AdminContext, uow: UnitOfWork, *, principal: str = "crucible") -> int:
    """Shred every retired directory older than the retention window (25). Called by
    the supervisor's retention step; idempotent."""
    cutoff = ctx.clock.now() - timedelta(hours=ctx.credential_retention_hours)
    shredded = 0
    for harness, source in ctx.credential_sources.items():
        current = Path(source.path)
        parent = current.parent
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            if not candidate.name.startswith(current.name + RETIRED_MARK):
                continue
            stamp = candidate.name[len(current.name) + len(RETIRED_MARK) :]
            try:
                retired_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=cutoff.tzinfo
                )
            except ValueError:
                continue
            if retired_at > cutoff:
                continue
            try:
                result = shred_tree(candidate, keep_root=False)
            except ShredIncompleteError as exc:
                # One directory that will not go never stops the sweep reaching the rest,
                # and it is recorded rather than retried silently on the next tick.
                record_refusal(
                    ctx,
                    principal=principal,
                    operation="credential retention sweep",
                    detail=str(exc.detail),
                )
                continue
            shredded += 1
            admin_event(
                uow,
                ctx,
                EventKind.CREDENTIAL_RETIRED_SHREDDED,
                principal=principal,
                reason=f"retention window of {ctx.credential_retention_hours} h elapsed",
                before={"retained": candidate.name},
                after={"retained": None},
                harness=harness,
                shredded=result,
            )
    return shredded
