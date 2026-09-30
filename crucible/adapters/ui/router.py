from __future__ import annotations

import os
import tomllib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui import actions, session
from crucible.adapters.ui.pages import audit as audit_ui
from crucible.adapters.ui.pages import bootstrap as bootstrap_ui
from crucible.adapters.ui.pages import credentials as credentials_ui
from crucible.adapters.ui.pages import dashboard as dashboard_ui
from crucible.adapters.ui.pages import gateway as gateway_ui
from crucible.adapters.ui.pages import github as github_ui
from crucible.adapters.ui.pages import harnesses as harnesses_ui
from crucible.adapters.ui.pages import images as images_ui
from crucible.adapters.ui.pages import repositories as repositories_ui
from crucible.adapters.ui.pages import retention as retention_ui
from crucible.adapters.ui.pages import routing as routing_ui
from crucible.adapters.ui.pages import tasks as tasks_ui
from crucible.adapters.ui.pages import tokens as tokens_ui
from crucible.adapters.ui.pages import wakes as wakes_ui
from crucible.adapters.ui.pages import workers as workers_ui
from crucible.adapters.ui.render import (
    _page,
)
from crucible.adapters.ui.session import (
    _require,
)

router = APIRouter(prefix="/ui", include_in_schema=False)


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
router.routes.extend(tasks_ui.router.routes)
router.routes.extend(wakes_ui.router.routes)
router.routes.extend(retention_ui.router.routes)
router.routes.extend(audit_ui.router.routes)
router.routes.extend(bootstrap_ui.router.routes)
