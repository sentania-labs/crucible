from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import crucible
from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.render import _operator_label, _page, _panel, _safe_value
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    status,
)
from crucible.application.errors import (
    ConflictError,
)

router = APIRouter(prefix="/ui", include_in_schema=False)


def _readiness_sections(readiness: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    """crucible#123: the to-do list from the status document's `readiness` part, which is
    computed from the same state the other pages show. One row per missing step, each
    with the page that fixes it; test fixtures are not in it. The per-harness list goes
    behind Details (crucible#115); its one-line summary is returned for the Service table."""
    todo = [[step["text"], step["fix"]] for step in readiness["steps"]]
    rows: list[list[Any]] = []
    for harness in readiness["harnesses"]:
        # A harness's own gaps are the to-do list only while none is ready; once one is,
        # the others' gaps are not what stands before a task (they stay under Details).
        if not readiness["ready_harnesses"]:
            todo.extend([step["text"], step["fix"]] for step in harness["steps"])
        if harness["steps"]:
            rows.extend(
                [harness["name"], "not ready", step["text"], step["fix"]]
                for step in harness["steps"]
            )
        else:
            rows.append(
                [
                    harness["name"],
                    "ready" if harness["state"] == "ready" else "off",
                    harness["note"],
                    "",
                ]
            )
    sections: list[dict[str, Any]] = []
    if todo:
        sections.append(
            {"title": "Before a task can run", "columns": ["Action", "Fix page"], "rows": todo}
        )
    ready = readiness["ready_harnesses"]
    summary = [
        "Harnesses",
        {
            "kind": "status",
            "value": f"ready: {', '.join(ready)}" if ready else "none ready",
            "tone": "ok" if ready else "warn",
        },
        {"kind": "link", "href": "/ui/harnesses", "label": "Open Harnesses"},
    ]
    detail = {
        "title": "Harness readiness",
        "columns": ["Harness", "State", "What is missing", "Fix page"],
        "rows": rows,
    }
    return sections, [summary, detail]


PROVIDER_TONES = {"ok": "ok", "degraded": "warn", "unavailable": "bad"}


def _provider_detail(item: dict[str, Any]) -> str:
    """The one check an operator would act on: the first that failed, else capacity."""
    checks = item.get("checks") or {}
    for key, value in checks.items():
        if value is False or (isinstance(value, str) and "fail" in value.lower()):
            return f"{_operator_label(key)}: {_safe_value(key, value)}"
    capacity = (item.get("capabilities") or {}).get("max_concurrency")
    return f"up to {capacity} workers at once" if capacity else ""


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    document = await status.status(ctx.admin, uow)
    readiness = document["readiness"]
    sections, (harness_summary, harness_detail) = _readiness_sections(readiness)
    supervisor = document["supervisor"]
    tasks_part = document["tasks"]
    attention = sum(len(rows) for rows in tasks_part["lists"].values())
    running = len(document["workers"])
    overview: list[list[Any]] = [
        [
            "Version",
            {
                "kind": "status",
                "value": crucible.__version__,
                "tone": "ok",
            },
            "",
        ],
        [
            "Supervisor",
            {
                "kind": "status",
                "value": "healthy" if supervisor["healthy"] else "not healthy",
                "tone": "ok" if supervisor["healthy"] else "bad",
            },
            {
                "kind": "note",
                "value": supervisor["health_detail"] if not supervisor["healthy"] else "",
                "hint": f"last tick {supervisor['last_tick_at'] or 'never'}",
            },
        ],
        *[
            [
                f"Provider: {item['name']}",
                {
                    "kind": "status",
                    "value": item["health"],
                    "tone": PROVIDER_TONES.get(str(item["health"]), "warn"),
                },
                _provider_detail(item),
            ]
            for item in document["providers"]
        ],
        [
            "Work",
            {
                "kind": "status",
                "value": f"{attention} need attention" if attention else "nothing waiting",
                "tone": "warn" if attention else "ok",
            },
            {"kind": "link", "href": "/ui/tasks", "label": f"{running} running; open Tasks"},
        ],
        [
            "Wakes",
            {
                "kind": "status",
                "value": f"{document['wakes']['unacked']} pending",
                "tone": "warn" if document["wakes"]["unacked"] else "ok",
            },
            {"kind": "link", "href": "/ui/wakes", "label": "Open Wakes"},
        ],
        harness_summary,
    ]
    internals = {
        key: value for key, value in supervisor.items() if key not in ("providers", "counts")
    }
    sections.append(
        {
            "title": "Service",
            "columns": ["Part", "State", ""],
            "rows": overview,
            "details": [
                harness_detail,
                {"title": "Supervisor", "panel": _panel(internals)},
                *[
                    {"title": f"Provider {item['name']} checks", "panel": _panel(item["checks"])}
                    for item in document["providers"]
                ],
            ],
        }
    )
    ready = readiness["ready"]
    ready_names = ", ".join(readiness["ready_harnesses"])
    return _page(
        request,
        principal,
        csrf,
        active="/ui",
        heading="Status",
        intro=(
            f"Ready for a task on {ready_names}."
            if ready
            else "Crucible cannot run a task yet. The list below says what it needs."
        ),
        sections=sections,
        badge="ready" if ready else "attention needed",
        badge_kind="ok" if ready else "warn",
    )
