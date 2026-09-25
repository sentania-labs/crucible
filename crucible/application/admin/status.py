"""The status document (25): one document, each part also its own resource. Sanitized
throughout: booleans, enumerations, timestamps, hashes of non-secret metadata."""

from __future__ import annotations

from typing import Any

from crucible.application.admin import audit, bootstrap, github
from crucible.application.admin.context import AdminContext
from crucible.application.admin.harnesses import list_harnesses, list_images
from crucible.application.admin.providers import providers_status
from crucible.application.queries import supervisor_view
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.repository import UnitOfWork

LISTED_STATES = (
    TaskState.BLOCKED,
    TaskState.PRE_PR_GATES_FAILED,
    TaskState.PUBLISH_FAILED,
    TaskState.CI_CERTIFICATION_FAILED,
    TaskState.HEAD_DIVERGED,
)
CAPABILITY_PARTS = ("harnesses", "providers", "github", "workers", "tasks", "wakes")


def workers(uow: UnitOfWork, *, owner: str | None = None) -> list[dict[str, Any]]:
    """Active attempts; with `owner`, only those of tasks that principal submitted."""
    out: list[dict[str, Any]] = []
    for attempt in uow.attempts.list_in_states(
        [
            AttemptState.PREPARING,
            AttemptState.LAUNCHING,
            AttemptState.RUNNING,
            AttemptState.TERMINATING,
        ]
    ):
        execution = uow.executions.get(attempt.execution_id)
        task = uow.tasks.get(attempt.task_id)
        if owner is not None and (task is None or task.principal_id != owner):
            continue
        out.append(
            {
                "attempt_id": attempt.id,
                "state": attempt.state.value,
                "task_id": attempt.task_id,
                "external_id": task.external_id if task else None,
                "harness": execution.harness if execution else None,
                "model": execution.model if execution else None,
                "image_digest": attempt.image_digest,
                "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                "last_heartbeat": attempt.log_resume_ts.isoformat()
                if attempt.log_resume_ts
                else None,
            }
        )
    return out


def tasks(uow: UnitOfWork, *, owner: str | None = None) -> dict[str, Any]:
    """Counts by state and the listed states; with `owner`, only that principal's tasks."""
    counts: dict[str, int] = {}
    lists: dict[str, list[dict[str, Any]]] = {}
    for state in TaskState:
        rows = [
            t for t in uow.tasks.list_by_state(state) if owner is None or t.principal_id == owner
        ]
        if rows:
            counts[state.value] = len(rows)
        if state in LISTED_STATES:
            lists[state.value] = [
                {"id": t.id, "external_id": t.external_id, "updated_at": t.updated_at.isoformat()}
                for t in rows
            ]
    return {"counts": counts, "lists": lists}


def wakes(uow: UnitOfWork, *, owner: str | None = None) -> dict[str, Any]:
    """Pending wakes per principal; with `owner`, that principal's alone, and `unacked`
    counts only its own."""
    per_principal: dict[str, int] = {}
    oldest: str | None = None
    for principal in uow.principals.list_all():
        if owner is not None and principal.id != owner:
            continue
        pending = uow.wakes.list_for_principal(
            principal.id, since=None, include_acked=False, limit=200
        )
        if pending:
            per_principal[principal.name] = len(pending)
            first = min(w.created_at for w in pending).isoformat()
            oldest = first if oldest is None or first < oldest else oldest
    return {
        "pending": per_principal,
        "oldest_pending": oldest,
        "unacked": uow.wakes.count_unacked() if owner is None else sum(per_principal.values()),
    }


def retention(uow: UnitOfWork) -> dict[str, Any]:
    recent = list(uow.retention.list_recent(50))
    last = max((a.acted_at for a in recent), default=None)
    return {
        "last_run": last.isoformat() if last else None,
        "recent_actions": len(recent),
        "kinds": sorted({a.kind for a in recent}),
    }


async def status(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    images = await list_images(ctx)
    harnesses = list_harnesses(ctx, uow, [i for _, i in images])
    credentials = {h["name"]: h["credential"] for h in harnesses}
    supervisor = supervisor_view(
        uow, list(ctx.providers.values()), ctx.clock.now(), ctx.lease_ttl_seconds
    ).model_dump(mode="json")
    return {
        "harnesses": harnesses,
        "credentials": credentials,
        "providers": await providers_status(ctx),
        "github": github.status(ctx, uow),
        "supervisor": supervisor,
        "workers": workers(uow),
        "tasks": tasks(uow),
        "wakes": wakes(uow),
        "retention": retention(uow),
        # 25: the bootstrap import is CLI-and-API in C6; its state is exposed here.
        "bootstrap": bootstrap.status_part(uow),
        "audit": {"cursor": audit.tail(uow, cursor=None, limit=1)["next_cursor"]},
    }


async def capabilities(ctx: AdminContext, uow: UnitOfWork, principal: Principal) -> dict[str, Any]:
    """25: the orchestrator's read-only view: harnesses, providers, github health,
    workers, tasks, wakes; nothing it could mutate and no credential detail beyond the
    state enumeration.

    An orchestrator sees its own work only: the workers, tasks and wakes parts are
    filtered to the tasks it submitted and the wakes addressed to it (crucible#40). An
    operator is exempt, as it is from task ownership (04). Harness, provider and GitHub
    health are the service's and not any one principal's, so every caller sees them."""
    document = await status(ctx, uow)
    view = {part: document[part] for part in CAPABILITY_PARTS}
    if principal.role is not Role.OPERATOR:
        view["workers"] = workers(uow, owner=principal.id)
        view["tasks"] = tasks(uow, owner=principal.id)
        view["wakes"] = wakes(uow, owner=principal.id)
    for harness in view["harnesses"]:
        harness["credential"] = {
            "state": harness["credential"]["state"],
            "session_compatibility": harness["credential"]["session_compatibility"],
        }
    view["github"] = {
        "configured": view["github"]["configured"],
        "key_present": view["github"]["key_present"],
        "repositories": [
            {"repository": r["repository"], "installation_covers": r["installation_covers"]}
            for r in view["github"]["repositories"]
        ],
    }
    return view
