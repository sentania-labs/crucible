from __future__ import annotations

import os
import tomllib
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui import actions, session
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.pages import credentials as credentials_ui
from crucible.adapters.ui.pages import dashboard as dashboard_ui
from crucible.adapters.ui.pages import gateway as gateway_ui
from crucible.adapters.ui.pages import github as github_ui
from crucible.adapters.ui.pages import harnesses as harnesses_ui
from crucible.adapters.ui.pages import images as images_ui
from crucible.adapters.ui.pages import repositories as repositories_ui
from crucible.adapters.ui.pages import routing as routing_ui
from crucible.adapters.ui.pages import tokens as tokens_ui
from crucible.adapters.ui.pages import workers as workers_ui
from crucible.adapters.ui.render import (
    _document_section,
    _page,
    _redirect,
    _state_words,
)
from crucible.adapters.ui.session import (
    _admin,
    _csrf,
    _form,
    _require,
)
from crucible.application.admin import (
    audit,
    bootstrap,
    status,
)
from crucible.application.decisions import record_decision
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
    NotFoundError,
)
from crucible.application.queries import pull_request_view, task_view
from crucible.contracts.api import (
    DecisionRequest,
)
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import TaskState
from crucible.domain.waivers import ACCEPT_NO_CI, WAIVABLE_STATES, WAIVE_EXTERNAL_REVIEW

router = APIRouter(prefix="/ui", include_in_schema=False)


def _gate_steps(gates: list[dict[str, Any]]) -> dict[str, Any]:
    tones = {"pass": "ok", "fail": "bad", "error": "bad", "skipped": "accent"}
    return {
        "kind": "steps",
        "items": [
            {
                "name": f"{g['gate']} ({g['classification']})",
                "result": g["result"],
                "detail": g["detail"] if g["result"] in ("fail", "error") else "",
                "tone": (
                    "warn"
                    if g["classification"] == "advisory" and g["result"] in ("fail", "error")
                    else tones.get(g["result"], "accent")
                ),
            }
            for g in gates
        ],
    }


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    document = status.tasks(uow)
    attention = [
        [_task_link(item["id"], item["external_id"]), _state_words(state), item["updated_at"]]
        for state, items in document["lists"].items()
        for item in items
    ] + [
        # hades FDY-0133: a task in publishing whose publication cannot start says why.
        [
            _task_link(item["task_id"], item["external_id"]),
            f"Waiting to publish: {item['reason']}",
            item["waiting_since"],
        ]
        for item in document.get("publishing_waiting", [])
    ]
    # One bounded query, newest first: the page shows RECENT_TASK_ROWS and reads no more.
    recent = list(
        uow.tasks.recently_updated(
            since=ctx.clock.now() - timedelta(days=RECENT_TASK_DAYS), limit=RECENT_TASK_ROWS
        )
    )
    # hades FDY-0139: every task with a pull request under observation, each linking to
    # its page, where the operator can waive what the task is still waiting for.
    delivering = [
        [
            {"kind": "link", "href": f"/ui/tasks/{task.id}", "label": task.external_id or task.id},
            _state_words(task.state.value),
            task.updated_at.isoformat(),
        ]
        for state in DELIVERY_STATES
        for task in uow.tasks.list_by_state(state)
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tasks",
        heading="Tasks",
        intro="Tasks that need you, then every task by state.",
        sections=[
            {
                "title": "Needs attention",
                "empty": "No task needs attention.",
                "columns": ["Task", "Why", "Since"],
                "rows": attention,
            },
            {
                "title": "Pull requests in delivery",
                "empty": "No pull request is open for a task.",
                "columns": ["Task", "State", "Since"],
                "rows": delivering,
            },
            {
                # ADR 0024: every pre-PR gate marked blocking or advisory, and what the
                # reviewer is asked to weigh.
                "title": "Gates by task",
                "note": (
                    "A failed blocking gate stops the task. A failed advisory gate does "
                    "not: it is listed for the reviewer, who decides."
                ),
                "empty": "No task is waiting on its gates.",
                "columns": ["Task", "State", "Gates", "For the reviewer"],
                "rows": [
                    [
                        item["external_id"] or item["id"],
                        _state_words(item["state"]),
                        _gate_steps(item["gates"]),
                        {
                            "kind": "note",
                            "value": "; ".join(
                                f"{r['gate']}: {r['detail']}" for r in item["for_reviewer"]
                            )
                            or "nothing",
                        },
                    ]
                    for item in document.get("gates", [])
                ],
            },
            {
                "title": "Tasks by state",
                "empty": "No tasks yet.",
                "columns": ["State", "Tasks"],
                "rows": [
                    [_state_words(state), count]
                    for state, count in sorted(document["counts"].items())
                ],
            },
            {
                "title": "Recently updated",
                "note": (
                    f"Tasks updated in the last {RECENT_TASK_DAYS} days, newest first. Open "
                    "one for its branch, pull request and merge."
                ),
                "empty": f"No task was updated in the last {RECENT_TASK_DAYS} days.",
                "columns": ["Task", "State", "Updated"],
                "rows": [
                    [
                        _task_link(task.id, task.external_id),
                        _state_words(task.state.value),
                        task.updated_at.isoformat(),
                    ]
                    for task in recent
                ],
            },
        ],
    )


RECENT_TASK_DAYS = 14


RECENT_TASK_ROWS = 50


def _task_link(task_id: str, external_id: str | None) -> dict[str, str]:
    return {"kind": "link", "href": f"/ui/tasks/{quote(task_id)}", "label": external_id or task_id}


# The states a task page offers the operator's waivers from, in the order a PR moves.
DELIVERY_STATES = (
    TaskState.AWAITING_EXTERNAL_REVIEW,
    TaskState.EXTERNAL_FEEDBACK_RECEIVED,
    TaskState.AWAITING_CI_CERTIFICATION,
    TaskState.CI_CERTIFICATION_FAILED,
    TaskState.HEAD_DIVERGED,
    TaskState.READY_FOR_MERGE,
)


WAIVER_FORMS = (
    (
        WAIVE_EXTERNAL_REVIEW,
        "Waive the remaining external review rounds",
        "The task stops waiting for the external reviewer and goes on to CI. Use it when "
        "the reviewer will not review this pull request.",
        "the external reviewer did not review this pull request",
    ),
    (
        ACCEPT_NO_CI,
        "Accept that this repository has no CI",
        "With no check run or workflow run on the head, CI certification is skipped "
        "instead of waiting. A check that does run is still certified.",
        "this repository has no CI for this task",
    ),
)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    try:
        view = task_view(uow, task_id)
    except NotFoundError:
        return RedirectResponse(
            f"/ui/tasks?kind=bad&message={quote(f'No task {task_id}.')}", status_code=303
        )
    # hades FDY-0143: the task's paper trail on GitHub, beside what it is waiting for.
    delivery = view.delivery
    not_yet = "not yet"
    delivered_pr: Any = not_yet
    if delivery.pull_request_number is not None:
        label = f"#{delivery.pull_request_number}"
        delivered_pr = (
            {"href": delivery.pull_request_url, "label": label}
            if (delivery.pull_request_url or "").startswith("https://")
            else label
        )
    sections: list[dict[str, Any]] = [
        {
            "title": "Task",
            "columns": ["Field", "Value"],
            "rows": [
                ["Task", view.external_id],
                ["Title", view.title],
                ["State", _state_words(view.state.value)],
                ["Id", view.id],
                ["Repository", view.repository],
                ["Owner", view.principal],
                ["Accepted head", view.head_sha or "none yet"],
                ["Created", view.created_at.isoformat()],
                ["Updated", view.updated_at.isoformat()],
            ],
        },
        {
            "title": "Delivery",
            "note": "The task's paper trail on GitHub. Each line fills in when it happens.",
            "columns": ["What", "Value"],
            "rows": [
                ["Work branch", delivery.work_branch or not_yet],
                ["Pushed head", delivery.pushed_head or not_yet],
                [
                    "Pushed at",
                    delivery.pushed_at.isoformat()
                    if delivery.pushed_at
                    else ("not recorded" if delivery.pushed_head else not_yet),
                ],
                ["Pull request", delivered_pr],
                ["Pull request state", delivery.pull_request_state or not_yet],
                ["Merge commit", delivery.merge_sha or not_yet],
                ["Merged by", delivery.merged_by or not_yet],
                ["Merged at", delivery.merged_at.isoformat() if delivery.merged_at else not_yet],
            ],
        },
    ]
    try:
        record = pull_request_view(uow, task_id)
    except NotFoundError:
        record = None
    if record is not None:
        certification = record.ci_certifications[-1] if record.ci_certifications else None
        sections.append(
            {
                "title": "Pull request",
                "columns": ["Field", "Value"],
                "rows": [
                    ["Pull request", {"href": record.url, "label": f"#{record.number}"}],
                    ["State", record.state],
                    ["Head", record.head_sha],
                    [
                        "External review",
                        f"{record.completed_rounds} of {record.required_rounds} round(s)",
                    ],
                    [
                        "CI",
                        f"{certification.state}: {certification.detail}"
                        if certification
                        else "not certified yet",
                    ],
                    [
                        "Last polled",
                        record.last_polled_at.isoformat() if record.last_polled_at else "never",
                    ],
                ],
            }
        )
        sections.append(
            {
                "title": "Gates after the pull request",
                "empty": "No gate has been evaluated yet.",
                "columns": ["Gate", "Result", "Detail"],
                "rows": [
                    [gate.gate, gate.result, gate.detail]
                    for gate in record.gates
                    if gate.head_sha == view.head_sha
                ],
            }
        )
    waivers = [d for d in view.decisions if d.get("kind") in (WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI)]
    sections.append(
        {
            "title": "Operator waivers",
            "empty": "No waiver is recorded for this task.",
            "columns": ["Kind", "Reason", "By", "Recorded"],
            "rows": [
                [d.get("kind"), d.get("verbatim"), d.get("principal"), d.get("created_at")]
                for d in waivers
            ],
        }
    )
    if principal.role is Role.ADMIN and view.state in WAIVABLE_STATES:
        for kind, title, note, resolves in WAIVER_FORMS:
            sections.append(
                {
                    "title": title,
                    "note": note,
                    "form": {
                        "action": f"/ui/tasks/{task_id}/decisions",
                        "label": title,
                        "fields": [
                            {"kind": "hidden", "name": "kind", "value": kind},
                            {"kind": "hidden", "name": "resolves", "value": resolves},
                            {
                                # Not `reason`: this is the decision's verbatim record,
                                # always required, not the optional audit note.
                                "name": "verbatim",
                                "label": "Reason (required; recorded on the task)",
                                "required": True,
                            },
                        ],
                    },
                }
            )
    return _page(
        request,
        principal,
        csrf,
        active=f"/ui/tasks/{task_id}",
        heading=f"Task {view.external_id}",
        intro="The task, its pull request, and what it is waiting for.",
        sections=sections,
    )


@router.post("/tasks/{task_id}/decisions")
async def task_decision(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    """ADR 0025: the operator's waiver, recorded through the same decision service the
    API's `POST /v1/tasks/{id}/decisions` uses, so it is audited the same way."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    form["return_to"] = f"/ui/tasks/{task_id}"
    try:
        _csrf(form, csrf)
        _admin(principal)
        kind = form.get("kind", "")
        if kind not in (WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI):
            raise ConflictError(f"the task page records only waivers, not {kind!r}")
        reason = (form.get("verbatim") or "").strip()
        if not reason:
            raise ConflictError("a waiver needs a reason")
        record_decision(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            request=DecisionRequest(
                kind=kind,
                verbatim=reason,
                resolves=form.get("resolves") or kind,
            ),
        )
        uow.commit()
        return _redirect(form, f"Recorded: {kind}. The next supervisor tick acts on it.")
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")


@router.get("/wakes", response_class=HTMLResponse)
def wakes_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = uow.wakes.list_for_principal(principal.id, since=None, include_acked=True, limit=200)
    summary = status.wakes(uow)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/wakes",
        heading="Wakes",
        intro=(
            f"Notifications for {principal.name}. {summary['unacked']} pending across "
            "every principal."
        ),
        sections=[
            {
                "title": "Your wakes",
                "empty": "No wakes for you.",
                "columns": ["Why", "Created", "Acknowledged", ""],
                "rows": [
                    [
                        item.reason.replace("_", " ").capitalize(),
                        item.created_at.isoformat(),
                        item.acked_at.isoformat() if item.acked_at else "no",
                        {"kind": "more", "label": "Details", "value": item.payload},
                    ]
                    for item in rows
                ],
            },
        ],
    )


@router.get("/retention", response_class=HTMLResponse)
def retention_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    recent = list(uow.retention.list_recent(200))
    summary = status.retention(uow)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/retention",
        heading="Retention and cleanup",
        intro=f"What the cleanup sweep removed. Last run: {summary['last_run'] or 'never'}.",
        sections=[
            {
                "title": "Recent actions",
                "empty": "The sweep has not removed anything yet.",
                "columns": ["What", "Subject", "When", "Detail"],
                "rows": [
                    [
                        item.kind.replace("_", " ").capitalize(),
                        item.subject,
                        item.acted_at.isoformat(),
                        item.detail,
                    ]
                    for item in recent
                ],
            },
        ],
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    cursor = int(request.query_params.get("cursor", "0") or 0)
    document = audit.tail(uow, cursor=cursor, limit=100)
    more = document["next_cursor"] != cursor and bool(document["items"])
    return _page(
        request,
        principal,
        csrf,
        active="/ui/audit",
        heading="Audit",
        intro="Every administrative change and refusal: who, when, and why.",
        sections=[
            {
                "title": "Changes" if not cursor else f"Changes after {cursor}",
                "empty": "No administrative change recorded.",
                "columns": ["When", "What", "Who", "Reason", ""],
                "rows": [
                    [
                        item["ts"],
                        str(item["kind"]).replace("_", " ").capitalize(),
                        item["principal"],
                        (item["payload"] or {}).get("reason") or "none given",
                        {"kind": "more", "label": "Details", "value": item["payload"]},
                    ]
                    for item in document["items"]
                ],
            },
            *(
                [
                    {
                        "title": "More",
                        "rows": [
                            [
                                {
                                    "kind": "link",
                                    "href": f"/ui/audit?cursor={document['next_cursor']}",
                                    "label": "Next page",
                                }
                            ]
                        ],
                        "columns": [""],
                    }
                ]
                if more
                else []
            ),
        ],
    )


@router.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = bootstrap.list_imports(uow)
    admin = principal.role is Role.ADMIN

    def actions(item: dict[str, Any]) -> dict[str, Any]:
        # The row's own actions, never a typed import ID (crucible#127).
        entries: list[dict[str, Any]] = [
            {
                "kind": "link",
                "href": f"/ui/bootstrap/{quote(item['import_id'])}",
                "label": "Show",
            }
        ]
        if admin and item["state"] == "verified":
            entries.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/bootstrap-commit",
                    "label": "Commit",
                    "danger": True,
                    "reason": True,
                    "hidden": {"import_id": item["import_id"]},
                }
            )
            # ADR 0029: an import that will not be committed is withdrawn, so a fresh
            # export of the same ledger can be imported.
            entries.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/bootstrap-discard",
                    "label": "Discard",
                    "danger": True,
                    "reason": True,
                    "hidden": {"import_id": item["import_id"]},
                }
            )
        return {"kind": "actions", "items": entries}

    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        heading="Bootstrap imports",
        intro=(
            "Ledgers imported from Foundry, the commit that makes one authoritative, and the "
            "discard that withdraws one that will not be committed."
        ),
        sections=[
            {
                "title": "Imports",
                "empty": "No ledger has been imported.",
                "columns": ["Import", "State", "Tasks", "Verified", "Committed", ""],
                "rows": [
                    [
                        item["import_id"],
                        item["state"],
                        (item.get("counts") or {}).get("tasks", 0),
                        item["verified_at"],
                        item["committed_at"] or "no",
                        actions(item),
                    ]
                    for item in items
                ],
            }
        ],
    )


@router.get("/bootstrap/{import_id}", response_class=HTMLResponse)
def bootstrap_import_page(request: Request, import_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    try:
        document = bootstrap.show(uow, import_id)
    except ApplicationError as exc:
        return RedirectResponse(
            f"/ui/bootstrap?kind=bad&message={quote(exc.detail or exc.title)}", status_code=303
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        heading="Bootstrap manifest",
        intro=import_id,
        sections=[_document_section("Manifest", document)],
    )


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key in sorted(value):
            out.extend(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return out
    return [(prefix, value)]


# The settings-file keys the `kubernetes.egress` admin setting replaces at runtime.
_EGRESS_SEEDS = [
    ["kubernetes", key]
    for key in (
        "dns_namespace",
        "dns_pod_labels",
        "local_endpoint_namespace",
        "local_endpoint_pod_labels",
        "local_endpoint_port",
    )
]


def _setting_applies(path: str, settings: Any) -> bool:
    """Whether a restart-bound setting does anything on this deployment (crucible#125):
    a provider's settings apply only while it is enabled (its `enabled` row stays, so
    the page still says it is off), and a credential's directory settings do not apply
    where the credentials are Secrets the service owns (Kubernetes without Docker, ADR
    0015)."""
    parts = path.split(".")
    if parts[0] in ("docker", "kubernetes") and parts[-1] != "enabled":
        return bool(getattr(settings, parts[0]).enabled)
    secrets_held = settings.kubernetes.enabled and not settings.docker.enabled
    return not (parts[0] == "credentials" and secrets_held and parts[-1] in ("path", "source"))


# 26 and issue 61: the one restart-bound setting that widens what a worker can reach. The
# page intro already says every setting here is read at start (crucible#115).
_BROAD_EGRESS_REASON = (
    "On, a worker's egress is the public internet on 443 minus the denied ranges, GitHub "
    "included, instead of the resolved allowlist."
)


# hades #174: a harness's configuration entry is where it starts, not a lock.
_HARNESS_DEFAULT_REASON = (
    "The starting value only. Enable or disable the harness on Harnesses; an "
    "administrator's decision there wins and needs no restart."
)


def _settings_rows(settings: Any) -> list[list[Any]]:
    if settings is None or not hasattr(settings, "model_dump"):
        return []
    config_path = os.environ.get("CRUCIBLE_CONFIG") or settings.model_config.get("toml_file")
    file_document: dict[str, Any] = {}
    if config_path:
        try:
            with open(config_path, "rb") as handle:
                file_document = tomllib.load(handle)
        except OSError:
            pass
    sensitive = {"database.url", "wake.secret", "wake.webhook_url"}
    rows = []
    for path, value in _flatten(settings.model_dump(mode="json")):
        if not _setting_applies(path, settings):
            continue
        env_name = "CRUCIBLE_" + path.replace(".", "__").upper()
        cursor: Any = file_document
        in_file = True
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                in_file = False
                break
            cursor = cursor[part]
        source = "environment" if env_name in os.environ else "file" if in_file else "default"
        shown = value
        if path in sensitive:
            shown = "present" if value else "absent"
        elif isinstance(value, str) and "://" in value:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                hostname = parsed.hostname or ""
                if parsed.port is not None:
                    hostname += f":{parsed.port}"
                shown = urlunsplit(
                    (
                        parsed.scheme,
                        f"[credentials]@{hostname}",
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        reason = (
            "The value is never shown."
            if path in sensitive
            else "Seeds the egress selectors; edit them on Routing, where a saved value wins."
            if path.split(".")[:2] in _EGRESS_SEEDS
            else "Seeds the short-role timeout; edit it on Routing, where a saved value wins."
            if path == "kubernetes.role_timeout_seconds"
            else _BROAD_EGRESS_REASON
            if path == "kubernetes.broad_egress"
            else _HARNESS_DEFAULT_REASON
            if path.startswith("harnesses.") and path.endswith(".enabled")
            else ""
        )
        rows.append([path, shown, source, reason])
    return rows


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = _settings_rows(ctx.settings)
    # Lead with what this deployment set; the defaults it left alone go behind a click
    # (crucible#115).
    chosen = [
        [path, value, source, note] for path, value, source, note in rows if source != "default"
    ]
    defaults = [[path, value, note] for path, value, source, note in rows if source == "default"]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/settings",
        heading="Settings",
        intro=(
            "Read when the service starts: change one in the settings file or the "
            "environment and restart. What can change while running is on Routing, "
            "Harnesses and Images."
        ),
        sections=[
            {
                "title": "Set on this deployment",
                "empty": "Every setting is at its default.",
                "columns": ["Setting", "Value", "Set in", "Note"],
                "rows": chosen,
                "details_label": f"Defaults left unchanged ({len(defaults)})",
                "details": [
                    {"title": "Defaults", "columns": ["Setting", "Value", "Note"], "rows": defaults}
                ],
            }
        ],
    )


async def _actions(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    if action == "bootstrap-commit":
        bootstrap.commit(
            ctx.admin,
            uow,
            principal=principal.name,
            import_id=form.get("import_id", ""),
            reason=reason,
        )
    elif action == "bootstrap-discard":
        bootstrap.discard(
            ctx.admin,
            uow,
            principal=principal.name,
            import_id=form.get("import_id", ""),
            reason=reason,
        )
    return None


register("bootstrap-commit", _actions)
register("bootstrap-discard", _actions)

router.routes.extend(session.router.routes)
router.routes.extend(actions.router.routes)
router.routes.extend(dashboard_ui.router.routes)
router.routes.extend(harnesses_ui.router.routes)
router.routes.extend(credentials_ui.router.routes)
router.routes.extend(gateway_ui.router.routes)
router.routes.extend(images_ui.router.routes)
router.routes.extend(routing_ui.router.routes)
router.routes.extend(repositories_ui.router.routes)
router.routes.extend(tokens_ui.router.routes)
router.routes.extend(github_ui.router.routes)
router.routes.extend(workers_ui.router.routes)
