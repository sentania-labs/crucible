"""The bootstrap ledger handoff (15): submit a `BootstrapExportV1` bundle, which is
validated in full and written in one transaction under a `bootstrap_imports` row in
state `verified`; show and list imports; commit one, which makes it authoritative and
records the handoff on every imported task.

Both entry points (the API under `/v1/import/bootstrap` and `crucible-admin bootstrap`)
call these functions. Submit and commit are administrative mutations (25): a reason and
a live supervisor lease, one event each with the principal and a before-and-after
summary. A bundle that fails validation returns every problem and stores nothing.

The synthetic `bootstrap` execution and its unsupervised attempt (an imported task that
was running when the ledger was exported) live in tables fenced to the supervisor (14),
so the import writes them under the live supervisor's own fenced token, read inside the
same transaction; a takeover between the guard and the insert rejects the write and the
whole import rolls back, which is the fence doing its job.
"""

from __future__ import annotations

import json
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
)
from crucible.application.errors import (
    BootstrapBundleError,
    ConflictError,
    NotFoundError,
    SupervisorNotLiveError,
)
from crucible.application.transitions import record_event
from crucible.domain.bootstrap import (
    BOOTSTRAP_PROVIDER,
    TaskRecord,
    ValidatedBundle,
    problem,
    validate_bundle,
)
from crucible.domain.entities import (
    Attempt,
    BootstrapImport,
    Event,
    Execution,
    ExecutionRole,
    Principal,
    Repository,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import AttemptState, ExecutionState
from crucible.domain.secrets import scan_text
from crucible.ports.repository import UnitOfWork

STATE_VERIFIED = "verified"
STATE_AUTHORITATIVE = "authoritative"
# Where an imported task lands when its `repository` names nothing registered (or
# nothing at all). A registration is an operator act (04); the import never invents one
# with a URL, so the sentinel carries none and the report names every task on it.
SENTINEL_REPOSITORY = "bootstrap"
SENTINEL_URL = "bootstrap://foundry-ledger"
DEFAULT_POLICY = "default-software"
HARNESS_MAX = 32
MODEL_MAX = 128

# What an imported source event's fields become. Stated once in every report so the
# reader knows where each one went (15 step 4).
EVENT_FIELD_MAP: list[dict[str, str]] = [
    {"field": "seq", "carried_as": "payload", "detail": "a new global seq; the original in seq"},
    {"field": "ts", "carried_as": "column", "detail": "normalized to UTC; the original in ts"},
    {"field": "task", "carried_as": "column", "detail": "task_id; the external id in task"},
    {"field": "event", "carried_as": "payload", "detail": "kind is bootstrap_event_imported"},
    {"field": "who", "carried_as": "payload", "detail": "principal is the importer"},
    {"field": "detail", "carried_as": "payload", "detail": "verbatim"},
]


def _summary(record: BootstrapImport) -> dict[str, Any]:
    manifest = record.manifest
    return {
        "import_id": record.id,
        "state": record.state,
        "content_sha256": record.content_sha256,
        "counts": manifest.get("counts", {}),
        "principal": manifest.get("principal"),
        "verified_at": record.verified_at.isoformat(),
        "committed_at": record.committed_at.isoformat() if record.committed_at else None,
    }


def show(uow: UnitOfWork, import_id: str) -> dict[str, Any]:
    record = uow.bootstrap_imports.get(import_id)
    if record is None:
        raise NotFoundError(f"bootstrap import {import_id} not found")
    return dict(record.manifest)


def list_imports(uow: UnitOfWork) -> list[dict[str, Any]]:
    return [_summary(r) for r in uow.bootstrap_imports.list_all()]


def status_part(uow: UnitOfWork) -> dict[str, Any]:
    """25: the API exposes the import state. The status document's `bootstrap` part."""
    imports = list_imports(uow)
    authoritative = next(
        (i["import_id"] for i in imports if i["state"] == STATE_AUTHORITATIVE), None
    )
    return {"authoritative": authoritative, "imports": imports}


def _policy(uow: UnitOfWork) -> tuple[str, int]:
    """The newest default-software version that is not retired: what the imported tasks
    are recorded under, since the ledger carries no policy reference of its own."""
    versions = [p for p in uow.policies.list_versions(DEFAULT_POLICY) if p.retired_at is None]
    if not versions:
        raise ConflictError(f"no {DEFAULT_POLICY} policy version is in force")
    newest = max(versions, key=lambda p: p.version)
    return newest.name, newest.version


def _sentinel(uow: UnitOfWork, ctx: AdminContext, *, principal: str) -> Repository:
    existing = uow.repositories.get_by_name(SENTINEL_REPOSITORY)
    if existing is not None:
        return existing
    return uow.repositories.upsert(
        Repository(
            id=new_id(),
            name=SENTINEL_REPOSITORY,
            url=SENTINEL_URL,
            default_branch="main",
            policy_name=DEFAULT_POLICY,
            installation_id=None,
            registered_by=principal,
            created_at=ctx.clock.now(),
        )
    )


def _resolve_owner(
    uow: UnitOfWork, *, principal: str, owner: str | None, problems: list[dict[str, str]]
) -> Principal | None:
    name = owner or principal
    found = uow.principals.get_by_name(name)
    if found is None:
        problems.append(problem("principal", f"no principal named {name!r}"))
    return found


def _scan(bundle: ValidatedBundle, problems: list[dict[str, str]]) -> None:
    """12 and 25: the records are served back by the API, so a secret-shaped value is
    refused by path and pattern, never stored and never echoed."""
    for index, task in enumerate(bundle.tasks):
        hit = scan_text(json.dumps(task.record, ensure_ascii=False))
        if hit is not None:
            problems.append(
                problem(f"tasks[{index}]", f"matched the {hit} pattern; refused, not stored")
            )
    for index, event in enumerate(bundle.events):
        hit = scan_text(json.dumps(event.record, ensure_ascii=False))
        if hit is not None:
            problems.append(
                problem(f"events[{index}]", f"matched the {hit} pattern; refused, not stored")
            )


def _text_column(value: object, *, width: int, field: str, task: TaskRecord) -> str:
    if isinstance(value, str) and value and len(value) <= width:
        return value
    if value not in (None, ""):
        task.uncarried.append(
            {
                "field": field,
                "carried_as": "record",
                "detail": f"not a string of at most {width} characters; the execution says "
                f"{BOOTSTRAP_PROVIDER!r}",
            }
        )
    return BOOTSTRAP_PROVIDER


def _write_task(
    uow: UnitOfWork,
    ctx: AdminContext,
    *,
    import_id: str,
    principal: str,
    owner: Principal,
    task: TaskRecord,
    repository: Repository,
    sentinel: bool,
    policy: tuple[str, int],
) -> dict[str, Any]:
    entity = Task(
        id=new_id(),
        external_id=task.external_id,
        principal_id=owner.id,
        project=task.project,
        title=task.title,
        state=task.state,
        contract_version=0,
        policy_name=policy[0],
        policy_version=policy[1],
        repository_id=repository.id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.updated_at if task.closed else None,
    )
    uow.tasks.add(entity)
    if sentinel and task.repository is not None:
        task.uncarried.append(
            {
                "field": "repository",
                "carried_as": "sentinel",
                "detail": f"{task.repository!r} is not a registered repository; the task is on "
                f"{SENTINEL_REPOSITORY!r}",
            }
        )
    entry: dict[str, Any] = {
        "external_id": task.external_id,
        "task_id": entity.id,
        "source_state": task.source_state,
        "state": task.state.value,
        "repository": repository.name,
        "unsupervised": task.unsupervised,
        "execution_id": None,
        "attempt_id": None,
    }
    if task.unsupervised:
        execution = Execution(
            id=new_id(),
            task_id=entity.id,
            role=ExecutionRole.IMPLEMENT,
            contract_version=0,
            harness=_text_column(
                task.record.get("harness"), width=HARNESS_MAX, field="harness", task=task
            ),
            model=_text_column(task.record.get("model"), width=MODEL_MAX, field="model", task=task),
            effort=None,
            provider=BOOTSTRAP_PROVIDER,
            image=BOOTSTRAP_PROVIDER,
            policy_snapshot={"bootstrap": {"import_id": import_id, "unsupervised": True}},
            state=ExecutionState.ACTIVE,
            max_attempts=1,
            retry_on=[],
            timeout_seconds=0,
            created_at=task.created_at,
        )
        uow.executions.add(execution)
        attempt = Attempt(
            id=new_id(),
            execution_id=execution.id,
            task_id=entity.id,
            number=1,
            state=AttemptState.RUNNING,
            created_at=task.created_at,
            unsupervised=True,
        )
        uow.attempts.add(attempt)
        marker = {"bootstrap": True, "import_id": import_id, "unsupervised": True}
        record_event(
            uow,
            ctx.clock,
            EventKind.EXECUTION_CREATED,
            principal=principal,
            task_id=entity.id,
            execution_id=execution.id,
            payload={**marker, "provider": BOOTSTRAP_PROVIDER, "source_state": task.source_state},
        )
        record_event(
            uow,
            ctx.clock,
            EventKind.ATTEMPT_CREATED,
            principal=principal,
            task_id=entity.id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={**marker, "number": 1, "state": AttemptState.RUNNING.value},
        )
        entry["execution_id"] = execution.id
        entry["attempt_id"] = attempt.id
    record_event(
        uow,
        ctx.clock,
        EventKind.BOOTSTRAP_TASK_IMPORTED,
        principal=principal,
        task_id=entity.id,
        execution_id=entry["execution_id"],
        attempt_id=entry["attempt_id"],
        payload={
            "import_id": import_id,
            "external_id": task.external_id,
            "source_state": task.source_state,
            "state": task.state.value,
            "record": task.record,
            "uncarried": list(task.uncarried),
        },
    )
    return entry


def submit(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    bundle: object,
    reason: str | None,
    owner: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """15 steps 1 to 4. Returns the verification report and whether this call created
    the import: a bundle whose content hash is already imported is replayed, not
    imported twice, so a retried handoff is idempotent."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="bootstrap submit")
    validated, problems = validate_bundle(bundle)
    if validated is not None:
        existing = uow.bootstrap_imports.get_by_content(validated.content_sha256)
        if existing is not None:
            return dict(existing.manifest), False
        _scan(validated, problems)
    owner_principal = _resolve_owner(uow, principal=principal, owner=owner, problems=problems)
    if validated is not None and owner_principal is not None:
        for index, task in enumerate(validated.tasks):
            if uow.tasks.get_by_external_id(owner_principal.id, task.external_id) is not None:
                problems.append(
                    problem(
                        f"tasks[{index}].id",
                        f"{task.external_id!r} already exists for principal "
                        f"{owner_principal.name!r}",
                    )
                )
    if problems or validated is None or owner_principal is None:
        record_refusal(
            ctx,
            principal=principal,
            operation="bootstrap submit",
            detail=f"the bundle failed validation with {len(problems)} problem(s); nothing stored",
        )
        raise BootstrapBundleError(
            f"the bundle failed validation with {len(problems)} problem(s); nothing was stored",
            errors=problems,
        )

    import_id = new_id()
    if any(t.unsupervised for t in validated.tasks):
        lease = uow.leases.get_supervisor()
        if lease is None:
            raise SupervisorNotLiveError("refused: no supervisor lease to write the attempt under")
        uow.set_fenced_token(lease.fenced_token)
    policy = _policy(uow)
    sentinel: Repository | None = None
    entries: list[dict[str, Any]] = []
    matched: dict[str, int] = {}
    on_sentinel: list[str] = []
    by_task_id: dict[str, str] = {}
    for task in validated.tasks:
        repository = uow.repositories.get_by_name(task.repository) if task.repository else None
        uses_sentinel = repository is None
        if repository is None:
            sentinel = sentinel or _sentinel(uow, ctx, principal=principal)
            repository = sentinel
            on_sentinel.append(task.external_id)
        else:
            matched[repository.name] = matched.get(repository.name, 0) + 1
        entry = _write_task(
            uow,
            ctx,
            import_id=import_id,
            principal=principal,
            owner=owner_principal,
            task=task,
            repository=repository,
            sentinel=uses_sentinel,
            policy=policy,
        )
        entries.append(entry)
        by_task_id[task.external_id] = entry["task_id"]
    first_seq: int | None = None
    last_seq: int | None = None
    for event in validated.events:
        written = uow.events.append(
            Event(
                seq=None,
                ts=event.ts,
                kind=EventKind.BOOTSTRAP_EVENT_IMPORTED.value,
                principal=principal,
                verified=False,
                payload={
                    "import_id": import_id,
                    "seq": event.seq,
                    "ts": event.ts_original,
                    "task": event.external_id,
                    "event": event.event,
                    "who": event.who,
                    "detail": event.detail,
                },
                task_id=by_task_id[event.external_id],
            )
        )
        first_seq = written.seq if first_seq is None else first_seq
        last_seq = written.seq
    now = ctx.clock.now()
    manifest: dict[str, Any] = {
        "import_id": import_id,
        "state": STATE_VERIFIED,
        "schema_version": validated.schema_version,
        "content_sha256": validated.content_sha256,
        "source": validated.source,
        "source_migrated": validated.source.get("migrated"),
        "principal": owner_principal.name,
        "imported_by": principal,
        "reason": reason,
        "verified_at": now.isoformat(),
        "committed_at": None,
        "policy": {"name": policy[0], "version": policy[1]},
        "counts": {
            "tasks": len(validated.tasks),
            "events": len(validated.events),
            "executions": sum(1 for t in validated.tasks if t.unsupervised),
            "attempts": sum(1 for t in validated.tasks if t.unsupervised),
            "handoff_events": 0,
        },
        "state_map": validated.state_map,
        "repositories": {"matched": matched, "sentinel": on_sentinel},
        "tasks": entries,
        "events": {"first_seq": first_seq, "last_seq": last_seq},
        "uncarried": {
            "tasks": [
                {"external_id": t.external_id, **entry}
                for t in validated.tasks
                for entry in t.uncarried
            ],
            "events": list(EVENT_FIELD_MAP),
        },
    }
    uow.bootstrap_imports.add(
        BootstrapImport(
            id=import_id,
            state=STATE_VERIFIED,
            schema_version=validated.schema_version,
            content_sha256=validated.content_sha256,
            source_sha256=str(validated.source.get("db_sha256", "")),
            source=validated.source,
            manifest=manifest,
            principal_id=owner_principal.id,
            imported_by=principal,
            verified_at=now,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.BOOTSTRAP_IMPORT_VERIFIED,
        principal=principal,
        reason=reason,
        before={"import": None},
        after={"import": import_id, "state": STATE_VERIFIED, "counts": manifest["counts"]},
        content_sha256=validated.content_sha256,
        source_migrated=manifest["source_migrated"],
        principal_for_tasks=owner_principal.name,
    )
    return manifest, True


def commit(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, import_id: str, reason: str | None
) -> dict[str, Any]:
    """15 step 5: the import becomes authoritative and every imported task records the
    handoff. Refused unless the import is `verified`, and refused while another import
    holds authority (ADR 0006: never more than one writable ledger)."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"bootstrap commit {import_id}"
    )
    record = uow.bootstrap_imports.get(import_id, for_update=True)
    if record is None:
        raise NotFoundError(f"bootstrap import {import_id} not found")
    if record.state != STATE_VERIFIED:
        record_refusal(
            ctx,
            principal=principal,
            operation=f"bootstrap commit {import_id}",
            detail=f"the import is {record.state}, not {STATE_VERIFIED}",
        )
        raise ConflictError(
            f"bootstrap import {import_id} is {record.state}; only a {STATE_VERIFIED} import "
            "can be committed"
        )
    holder = uow.bootstrap_imports.authoritative()
    if holder is not None:
        record_refusal(
            ctx,
            principal=principal,
            operation=f"bootstrap commit {import_id}",
            detail=f"import {holder.id} already holds authority",
        )
        raise ConflictError(
            f"bootstrap import {holder.id} already holds authority; there is never more than "
            "one authoritative ledger (ADR 0006)"
        )
    now = ctx.clock.now()
    handoffs = 0
    for entry in record.manifest.get("tasks", []):
        record_event(
            uow,
            ctx.clock,
            EventKind.BOOTSTRAP_HANDOFF,
            principal=principal,
            task_id=str(entry["task_id"]),
            payload={
                "import_id": import_id,
                "external_id": entry["external_id"],
                "reason": reason,
                "before": {"authority": "foundry-ledger"},
                "after": {"authority": "crucible", "state": entry["state"]},
            },
        )
        handoffs += 1
    manifest = dict(record.manifest)
    manifest["state"] = STATE_AUTHORITATIVE
    manifest["committed_at"] = now.isoformat()
    manifest["committed_by"] = principal
    manifest["commit_reason"] = reason
    manifest["counts"] = {**manifest.get("counts", {}), "handoff_events": handoffs}
    record.state = STATE_AUTHORITATIVE
    record.committed_at = now
    record.committed_by = principal
    record.manifest = manifest
    uow.bootstrap_imports.save(record)
    admin_event(
        uow,
        ctx,
        EventKind.BOOTSTRAP_IMPORT_COMMITTED,
        principal=principal,
        reason=reason,
        before={"import": import_id, "state": STATE_VERIFIED},
        after={"import": import_id, "state": STATE_AUTHORITATIVE, "handoff_events": handoffs},
        content_sha256=record.content_sha256,
    )
    return manifest
