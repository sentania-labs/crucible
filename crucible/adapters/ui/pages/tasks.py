from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.render import _page, _redirect, _state_words
from crucible.adapters.ui.session import _admin, _csrf, _form, _require
from crucible.application.admin import (
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
from crucible.domain.entities import Role
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


def _busy_fallthrough(attempt: Any) -> str | None:
    if attempt is None or attempt.model is None:
        return None
    skipped = [
        candidate
        for candidate in attempt.ordered_candidates
        if candidate.get("busy") and candidate.get("model") != attempt.model
    ]
    if not skipped:
        return None
    choices = ", ".join(f"{candidate['model']} on {candidate['harness']}" for candidate in skipped)
    verb = "was" if len(skipped) == 1 else "were"
    return (
        f"Ran on {attempt.model} on {attempt.harness}, its next available choice, "
        f"because {choices} {verb} busy."
    )


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
    fallthroughs = [
        (attempt.id, words)
        for execution in view.executions
        for attempt in execution.attempts
        if (words := _busy_fallthrough(attempt)) is not None
    ]
    if fallthroughs:
        sections.append(
            {
                "title": "Routing",
                "columns": ["Attempt", "Decision"],
                "rows": fallthroughs,
            }
        )
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
