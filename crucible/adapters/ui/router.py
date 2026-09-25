"""HTML administration adapter under ``/ui``."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.staticfiles import StaticFiles

from crucible.adapters.api.deps import Ctx, UoW
from crucible.application.admin import (
    audit,
    bootstrap,
    credentials,
    gateway,
    github,
    harnesses,
    images,
    login,
    repositories,
    routing,
    status,
    tokens,
)
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.admin.context import guard_mutation
from crucible.application.auth import authenticate
from crucible.application.errors import ApplicationError, ConflictError, ForbiddenError
from crucible.application.first_run import discard_after_use
from crucible.application.policies import put_policy, put_routing_policy
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.cluster_egress import format_labels, parse_labels
from crucible.domain.entities import Principal, Role
from crucible.domain.secrets import redact, scan_text
from crucible.ports.repository import UnitOfWork

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
router = APIRouter(prefix="/ui", include_in_schema=False)
static = StaticFiles(directory=str(ROOT / "static"))
COOKIE = "crucible_ui"
PREAUTH_COOKIE = "crucible_ui_preauth"
SESSION_MAX_AGE = 12 * 60 * 60
PREAUTH_MAX_AGE = 10 * 60
NAV = (
    ("/ui", "Status"),
    ("/ui/harnesses", "Harnesses"),
    ("/ui/credentials", "Credentials"),
    ("/ui/gateway", "Local gateway"),
    ("/ui/images", "Images"),
    ("/ui/routing", "Routing"),
    ("/ui/repositories", "Repositories"),
    ("/ui/tokens", "Tokens"),
    ("/ui/github", "GitHub"),
    ("/ui/workers", "Workers"),
    ("/ui/tasks", "Tasks"),
    ("/ui/wakes", "Wakes"),
    ("/ui/retention", "Retention"),
    ("/ui/audit", "Audit"),
    ("/ui/bootstrap", "Bootstrap"),
    ("/ui/settings", "Settings"),
)

LABELS = {
    "active": "Currently active",
    "api_base": "API base",
    "app_id": "App ID",
    "attempt_id": "Attempt ID",
    "authoritative": "Authoritative import",
    "checked_at": "Last checked",
    "clear_reason": "Clear reason",
    "cleared_at": "Cleared at",
    "cleared_by": "Cleared by",
    "committed_at": "Committed at",
    "configured": "App configured",
    "content_sha256": "Content fingerprint",
    "counts": "Tasks by state",
    "enabled_by_administrator": "Runtime gate",
    "enabled_by_configuration": "Configuration gate",
    "exhausted_at": "Exhausted at",
    "external_id": "External ID",
    "health_detail": "Health detail",
    "healthy": "Supervisor health",
    "holder": "Lease holder",
    "image_digest": "Image digest",
    "installation_covers": "Installation coverage",
    "installation_id": "Installation ID",
    "key_fingerprint": "Public key fingerprint",
    "key_present": "Private key",
    "last_check": "Last connectivity check",
    "last_error": "Last error",
    "last_error_at": "Last error time",
    "last_heartbeat": "Last heartbeat",
    "last_run": "Last cleanup action",
    "last_success_at": "Last successful tick",
    "last_tick_at": "Last tick",
    "lease": "Supervisor lease",
    "lists": "Tasks needing attention",
    "next_cursor": "Next cursor",
    "oldest_pending": "Oldest pending wake",
    "pending": "Deliveries pending by principal",
    "recent_actions": "Recent actions",
    "repositories": "Repositories",
    "reset_at": "Automatic reset at",
    "task_id": "Task ID",
    "tick_ms": "Tick duration (ms)",
    "unacked": "Deliveries pending",
    "updated_at": "Last updated",
    "verified_at": "Verified at",
    "webhook_enabled": "Webhook",
    "webhook_secret_present": "Webhook secret",
}

SECRET_PARTS = {
    "access_token",
    "authorization",
    "code",
    "credential_value",
    "device_code",
    "oauth_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
NON_SECRET_TOKEN_FIELDS = {
    "error_code",
    "exit_code",
    "fenced_token",
    "http_code",
    "status_code",
    "tokens_in",
    "tokens_out",
}


def _operator_label(key: str) -> str:
    """Turn an API key into an operator label while retaining the key separately."""
    if key in LABELS:
        return LABELS[key]
    words = key.replace(".", " ").replace("_", " ").split()
    expanded = [
        word.upper() if word.lower() in {"api", "id", "sha256", "url"} else word for word in words
    ]
    label = " ".join(expanded)
    return label[:1].upper() + label[1:]


def _secret_field(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    terminal = separated.rsplit(".", 1)[-1]
    lowered = separated.replace(".", "_")
    if lowered in NON_SECRET_TOKEN_FIELDS or terminal in NON_SECRET_TOKEN_FIELDS:
        return False
    if lowered.endswith("_present") or lowered.endswith("_fingerprint"):
        return False
    return any(
        part == lowered
        or lowered.startswith(f"{part}_")
        or lowered.endswith(f"_{part}")
        or f"_{part}_" in lowered
        for part in SECRET_PARTS
    )


def _safe_value(key: str, value: Any) -> Any:
    """Return readable scalar content without exposing secret-shaped values."""
    lowered = key.lower()
    if _secret_field(lowered):
        return "not displayed"
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            url_parameters = unquote(f"{parsed.query}&{parsed.fragment}")
            sensitive_parameters = any(
                _secret_field(partition.partition("=")[0])
                for partition in re.split(r"[&?;]", url_parameters)
                if partition
            )
            if parsed.username is not None or parsed.password is not None or sensitive_parameters:
                hostname = parsed.hostname or ""
                if parsed.port is not None:
                    hostname += f":{parsed.port}"
                return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
        except ValueError:
            return "invalid URL"
    if isinstance(value, bool):
        if lowered.endswith("healthy") or lowered == "healthy":
            return "healthy" if value else "not healthy"
        if lowered.endswith("present"):
            return "present" if value else "absent"
        if lowered.endswith("enabled") or lowered.startswith("enabled_"):
            return "enabled" if value else "disabled"
        if lowered in {"active", "configured", "installation_covers"}:
            words = {
                "active": ("active", "inactive"),
                "configured": ("configured", "not configured"),
                "installation_covers": ("covered", "not covered"),
            }[lowered]
            return words[0] if value else words[1]
        if lowered == "ok":
            return "successful" if value else "failed"
        return "yes" if value else "no"
    if value is None:
        return "none"
    if isinstance(value, str):
        return redact(value)
    return value


def _flatten_table_row(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    if not value:
        return {prefix or "value": _panel(value, key=prefix)}
    row: dict[str, Any] = {}
    for child_key, item in value.items():
        path = f"{prefix}.{child_key}" if prefix else str(child_key)
        if isinstance(item, dict):
            row.update(_flatten_table_row(item, path))
        elif isinstance(item, list):
            row[path] = _panel(item, key=path)
        else:
            row[path] = _safe_value(path, item)
    return row


def _panel(value: Any, *, key: str = "") -> dict[str, Any]:
    """Build the three readable panel kinds used by the administration template."""
    if isinstance(value, dict):
        items = []
        for child_key, child in value.items():
            item: dict[str, Any] = {
                "label": _operator_label(str(child_key)),
                "source": str(child_key),
            }
            if isinstance(child, (dict, list)):
                item["panel"] = _panel(child, key=str(child_key))
            else:
                item["value"] = _safe_value(str(child_key), child)
            items.append(item)
        return {"kind": "fields", "items": items}
    if isinstance(value, list):
        if not value:
            return {"kind": "empty"}
        if all(isinstance(item, dict) for item in value):
            flattened = [_flatten_table_row(item) for item in value]
            column_keys = list(dict.fromkeys(path for row in flattened for path in row))
            return {
                "kind": "table",
                "columns": [
                    {"label": _operator_label(path), "source": path} for path in column_keys
                ],
                "rows": [[row.get(path, "none") for path in column_keys] for row in flattened],
            }
        rows = [
            [_panel(item, key=key)] if isinstance(item, (dict, list)) else [_safe_value(key, item)]
            for item in value
        ]
        return {
            "kind": "table",
            "columns": [{"label": "Value", "source": key}],
            "rows": rows,
        }
    return {"kind": "value", "value": _safe_value(key, value)}


def _document_section(title: str, document: Any) -> dict[str, Any]:
    return {"title": title, "panel": _panel(document)}


templates.env.globals["panel_from_cell"] = _panel
templates.env.globals["safe_value"] = _safe_value


def _serializer(ctx: Any) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.ui_signing_key, salt="crucible-ui-session-v1")


def _preauth_serializer(ctx: Any) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.ui_signing_key, salt="crucible-ui-preauth-v1")


def _session(request: Request, ctx: Any, uow: UnitOfWork) -> tuple[Principal, str] | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        document = _serializer(ctx).loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get("token")
    csrf = document.get("csrf")
    if not isinstance(token, str) or not isinstance(csrf, str):
        return None
    principal = authenticate(uow, token)
    return (principal, csrf) if principal is not None else None


def _require(
    request: Request, ctx: Any, uow: UnitOfWork
) -> tuple[Principal, str] | RedirectResponse:
    found = _session(request, ctx, uow)
    if found is not None:
        return found
    return RedirectResponse(f"/ui/sign-in?next={quote(request.url.path)}", status_code=303)


def _base(
    request: Request,
    principal: Principal | None,
    csrf: str = "",
    *,
    title: str,
    active: str,
) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "active": active,
        "nav": NAV,
        "principal": principal,
        "csrf": csrf,
        "message": request.query_params.get("message"),
        "message_kind": request.query_params.get("kind", "info"),
    }


def _page(
    request: Request,
    principal: Principal,
    csrf: str,
    *,
    active: str,
    heading: str,
    intro: str,
    sections: list[dict[str, Any]],
    badge: str | None = None,
    badge_kind: str = "accent",
) -> HTMLResponse:
    timezone = "America/Chicago"
    settings = getattr(request.app.state.ctx, "settings", None)
    if settings is not None:
        timezone = settings.service.render_timezone
    sections = _localize(sections, timezone)
    context = _base(request, principal, csrf, title=heading, active=active)
    context.update(
        heading=heading,
        intro=intro,
        sections=sections,
        badge=badge,
        badge_kind=badge_kind,
    )
    return templates.TemplateResponse(request=request, name="page.html", context=context)


def _localize(value: Any, timezone: str) -> Any:
    """Render stored UTC instants in the operator's configured local zone."""
    if isinstance(value, dict):
        return {key: _localize(item, timezone) for key, item in value.items()}
    if isinstance(value, list):
        return [_localize(item, timezone) for item in value]
    if isinstance(value, tuple):
        return tuple(_localize(item, timezone) for item in value)
    moment: datetime | None = value if isinstance(value, datetime) else None
    if isinstance(value, str) and "T" in value:
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")

        def replace(match: re.Match[str]) -> str:
            try:
                parsed = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
            except ValueError:
                return match.group(0)
            try:
                local = parsed.astimezone(ZoneInfo(timezone))
            except ZoneInfoNotFoundError:
                local = parsed.astimezone(ZoneInfo("America/Chicago"))
            return local.strftime("%Y-%m-%d %I:%M:%S %p %Z")

        replaced = pattern.sub(replace, value)
        if replaced != value:
            return replaced
    if isinstance(value, str) and "T" in value and (value.endswith("Z") or "+" in value[10:]):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            moment = None
    if moment is None or moment.tzinfo is None:
        return value
    try:
        local = moment.astimezone(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        local = moment.astimezone(ZoneInfo("America/Chicago"))
    return local.strftime("%Y-%m-%d %I:%M:%S %p %Z")


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def _admin(principal: Principal) -> None:
    if principal.role is not Role.ADMIN:
        raise ForbiddenError("admin role required")


def _csrf(form: dict[str, str], expected: str) -> None:
    supplied = form.get("csrf", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ForbiddenError("the form expired or its CSRF token is invalid")


def _redirect(form: dict[str, str], message: str, *, kind: str = "ok") -> RedirectResponse:
    target = form.get("return_to", "/ui")
    if not target.startswith("/ui") or target.startswith("//"):
        target = "/ui"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{separator}kind={quote(kind)}&message={quote(message)}", status_code=303
    )


def _readiness_sections(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    """crucible#123: the to-do list from the status document's `readiness` part, which is
    computed from the same state the other pages show. One row per missing step, each
    with the page that fixes it; test fixtures are not in it."""
    sections: list[dict[str, Any]] = []
    if readiness["steps"]:
        sections.append(
            {
                "title": "Before a task can run",
                "columns": ["Action", "Fix page"],
                "rows": [[step["text"], step["fix"]] for step in readiness["steps"]],
            }
        )
    rows: list[list[Any]] = []
    for harness in readiness["harnesses"]:
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
    sections.append(
        {
            "title": "Harness readiness",
            "note": "What each harness still needs before a task can run on it.",
            "columns": ["Harness", "State", "What is missing", "Fix page"],
            "rows": rows,
        }
    )
    return sections


@router.get("/sign-in", response_class=HTMLResponse)
def sign_in_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    if _session(request, ctx, uow) is not None:
        return RedirectResponse("/ui", status_code=303)
    return _sign_in_form(request, ctx, next_path=request.query_params.get("next", "/ui"))


def _sign_in_form(
    request: Request,
    ctx: Any,
    *,
    next_path: str,
    message: str | None = None,
    status_code: int = 200,
) -> Response:
    csrf = os.urandom(24).hex()
    context = _base(request, None, title="Sign in", active="")
    context.update(
        next=next_path,
        csrf=csrf,
        message=message,
        message_kind="bad",
        first_run_where=ctx.first_run.where() if ctx.first_run is not None else None,
    )
    response = templates.TemplateResponse(
        request=request, name="signin.html", context=context, status_code=status_code
    )
    response.set_cookie(
        PREAUTH_COOKIE,
        _preauth_serializer(ctx).dumps({"csrf": csrf}),
        max_age=PREAUTH_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/ui/sign-in",
    )
    return response


@router.post("/sign-in")
async def sign_in(request: Request, ctx: Ctx, uow: UoW) -> Response:
    form = await _form(request)
    raw_preauth = request.cookies.get(PREAUTH_COOKIE, "")
    try:
        preauth = _preauth_serializer(ctx).loads(raw_preauth, max_age=PREAUTH_MAX_AGE)
        expected = preauth.get("csrf", "") if isinstance(preauth, dict) else ""
        _csrf(form, expected)
    except (BadSignature, SignatureExpired, ForbiddenError):
        return _sign_in_form(
            request,
            ctx,
            next_path=form.get("next", "/ui"),
            message="The sign-in form expired or its CSRF token is invalid.",
            status_code=403,
        )
    token = form.get("token", "")
    principal = authenticate(uow, token)
    if principal is None:
        return _sign_in_form(
            request,
            ctx,
            next_path=form.get("next", "/ui"),
            message="Token not recognized.",
            status_code=401,
        )
    # ADR 0016: the first-run token has done its job once it has signed someone in;
    # it does not stay in its Secret or file for the next reader.
    await asyncio.to_thread(discard_after_use, ctx.first_run, principal.name)
    csrf = os.urandom(24).hex()
    value = _serializer(ctx).dumps({"token": token, "csrf": csrf})
    target = form.get("next", "/ui")
    if not target.startswith("/ui") or target.startswith("//"):
        target = "/ui"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/ui",
    )
    response.delete_cookie(PREAUTH_COOKIE, path="/ui/sign-in", httponly=True, samesite="strict")
    return response


@router.post("/sign-out")
async def sign_out(request: Request, ctx: Ctx, uow: UoW) -> RedirectResponse:
    form = await _form(request)
    found = _session(request, ctx, uow)
    if found is not None:
        _csrf(form, found[1])
    response = RedirectResponse("/ui/sign-in", status_code=303)
    response.delete_cookie(COOKIE, path="/ui", httponly=True, samesite="strict")
    return response


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
    sections = _readiness_sections(readiness)
    sections.extend(
        [
            _document_section("Supervisor", document["supervisor"]),
            _document_section("Providers", document["providers"]),
            _document_section("Task state", document["tasks"]),
            _document_section("Pending wakes", document["wakes"]),
        ]
    )
    ready = readiness["ready"]
    ready_names = ", ".join(readiness["ready_harnesses"])
    return _page(
        request,
        principal,
        csrf,
        active="/ui",
        heading="System status",
        intro=(
            f"Ready for a task on {ready_names}."
            if ready
            else "One operational view of readiness, work, providers, and actions needed."
        ),
        sections=sections,
        badge="ready" if ready else "attention needed",
        badge_kind="ok" if ready else "warn",
    )


@router.get("/harnesses", response_class=HTMLResponse)
async def harness_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    discovered = await harnesses.list_images(ctx.admin)
    items, _ = await harnesses.read_harnesses(ctx.admin, uow, [item for _, item in discovered])
    rows = [
        [
            item["name"],
            item["enabled_by_configuration"],
            item["enabled_by_administrator"],
            item["reason"],
            item["credential"]["state"],
            item.get("concurrency_in_use", 0),
            item.get("images", []),
        ]
        for item in items
    ]
    sections: list[dict[str, Any]] = [
        {
            "title": "Harness roster",
            "columns": [
                "Harness",
                "Configured",
                "Runtime",
                "Reason",
                "Credential",
                "Concurrency",
                "Images",
            ],
            "rows": rows,
        }
    ]
    if principal.role is Role.ADMIN:
        sections.append(
            {
                "title": "Change runtime gate",
                "note": "The configuration gate is restart-bound and is shown on Settings.",
                "form": {
                    "action": "/ui/actions/harness",
                    "label": "Apply runtime gate",
                    "fields": [
                        {
                            "name": "harness",
                            "label": "Harness",
                            "kind": "select",
                            "options": [(item["name"], item["name"]) for item in items],
                        },
                        {
                            "name": "enabled",
                            "label": "State",
                            "kind": "select",
                            "options": [("true", "Enabled"), ("false", "Disabled")],
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/harnesses",
        heading="Harnesses",
        intro="Installed images, compatibility, and both launch gates.",
        sections=sections,
    )


@router.get("/credentials", response_class=HTMLResponse)
async def credentials_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    names = list(ctx.admin.harnesses.names())
    secrets = await credentials.read_secrets(ctx.admin, names)
    rows: list[list[Any]] = []
    for name in names:
        view = credentials.state_view(ctx.admin, uow, name, secrets.get(name))
        rows.append(
            [
                name,
                view.get("state"),
                gateway.plain_outcome(view.get("last_launch_outcome")),
                view.get("session_compatibility"),
                # Hermes has no login: its key and gateway URL are set together (#119).
                "/ui/gateway" if name == credentials.HERMES else f"/ui/credentials/{name}/login",
            ]
        )
    sections: list[dict[str, Any]] = [
        {
            "title": "Credential state",
            "note": "Hermes has no login: set its key with the gateway URL on Local gateway.",
            "columns": ["Harness", "State", "Last test", "Compatibility", "Set up"],
            "rows": rows,
        }
    ]
    if principal.role is Role.ADMIN:
        options = [(name, name) for name in names]
        sections.append(
            {
                "title": "Validate, probe, or remove",
                "form": {
                    "action": "/ui/actions/credential",
                    "label": "Run credential action",
                    "fields": [
                        {
                            "name": "harness",
                            "label": "Harness",
                            "kind": "select",
                            "options": options,
                        },
                        {
                            "name": "verb",
                            "label": "Action",
                            "kind": "select",
                            "options": [
                                ("validate", "Validate"),
                                ("probe", "Probe"),
                                ("rotate", "Rotate from prepared server directory"),
                                ("remove", "Remove"),
                            ],
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                        {
                            "name": "new_path",
                            "label": "Prepared directory (rotate only)",
                        },
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/credentials",
        heading="Credentials",
        intro="Sanitized state and onboarding for each harness. Values are never displayed.",
        sections=sections,
    )


@router.get("/credentials/{harness}/login", response_class=HTMLResponse)
def login_page(request: Request, harness: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    document = (
        {"harness": harness, "state": "not_required", "output_tail": []}
        if harness == "hermes"
        else login.login_status(ctx.logins, harness)
    )
    context = _base(request, principal, csrf, title=f"{harness} login", active="/ui/credentials")
    context.update(harness=harness, login=document)
    return templates.TemplateResponse(request=request, name="login.html", context=context)


CAPABILITY_OPTIONS = [("small", "small"), ("mid", "mid"), ("frontier", "frontier")]


@router.get("/gateway", response_class=HTMLResponse)
async def gateway_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """crucible#119, #121: the gateway URL and the Hermes key in one place, a test of
    both in plain words, and the gateway's own model list to pick from."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    secrets = await credentials.read_secrets(ctx.admin, [credentials.HERMES])
    view = gateway.gateway_view(ctx.admin, uow, secrets.get(credentials.HERMES))
    offered = await gateway.models_view(ctx.admin, uow)
    summary = {
        "endpoint_url": view["endpoint_url"] or "not set",
        "key": "set" if view["key_set"] else "not set",
        "credential_state": view["credential_state"],
        "last_test": view["last_test"],
        "last_tested_at": view["last_tested_at"],
    }
    sections: list[dict[str, Any]] = [_document_section("Gateway", summary)]
    listing: dict[str, Any] = {
        "title": "Models the key can see",
        "note": offered["error"]
        or (
            f"Gateway {offered['endpoint_url']} lists {offered['offered_count']} "
            "model(s) for this key. Tick the ones to use; saving writes a new routing "
            "policy version. A model the gateway no longer offers is disabled, not removed."
        ),
    }
    if principal.role is not Role.ADMIN:
        listing.update(
            columns=["Model", "Offered", "In use", "Thinking", "Capability", "Note"],
            rows=[
                [
                    row["id"],
                    row["offered"],
                    row["enabled"],
                    row["enable_thinking"],
                    row["capability"],
                    row["note"],
                ]
                for row in offered["models"]
            ],
        )
        sections.append(listing)
    else:
        sections.append(
            {
                "title": "Set the gateway URL and key",
                "note": (
                    "The URL is the gateway's OpenAI-compatible base, ending in /v1. The key "
                    "is the LiteLLM virtual key Hermes sends; it is written to the Hermes "
                    "credential (on Kubernetes the Secret Crucible owns, otherwise a file "
                    "mode 0600) and never shown or audited. Leave it empty to keep the key "
                    "already set. Saving tests both: the gateway's readiness check, then its "
                    "model list with the key."
                ),
                "form": {
                    "action": "/ui/actions/gateway-save",
                    "label": "Save and test",
                    "fields": [
                        {
                            "name": "endpoint_url",
                            "label": "Gateway URL",
                            "kind": "url",
                            "value": view["endpoint_url"] or "",
                            "placeholder": "https://llm.example.internal/v1",
                            "required": True,
                        },
                        {
                            "name": "api_key",
                            "label": "Key (empty keeps the current one)"
                            if view["key_set"]
                            else "Key",
                            "kind": "password",
                            "required": not view["key_set"],
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        if view["endpoint_url"]:
            sections.append(
                {
                    "title": "Test again",
                    "form": {
                        "action": "/ui/actions/gateway-test",
                        "label": "Test the gateway",
                        "fields": [{"name": "reason", "label": "Reason", "required": True}],
                    },
                }
            )
        if offered["models"]:
            rows = []
            for index, row in enumerate(offered["models"]):
                rows.append(
                    [
                        {"kind": "hidden", "name": f"model.{index}.id", "value": row["id"]},
                        {
                            "kind": "checkbox",
                            "name": f"model.{index}.enabled",
                            "value": row["enabled"],
                            "label": f"use {row['id']}",
                        },
                        {
                            "kind": "checkbox",
                            "name": f"model.{index}.thinking",
                            "value": row["enable_thinking"],
                            "label": f"thinking for {row['id']}",
                        },
                        {
                            "kind": "select",
                            "name": f"model.{index}.capability",
                            "value": row["capability"],
                            "options": CAPABILITY_OPTIONS,
                            "label": f"capability of {row['id']}",
                        },
                        {"value": row["note"]},
                    ]
                )
            listing["form"] = {
                "action": "/ui/actions/gateway-models",
                "label": "Save model choices",
                "fields": [
                    {
                        "kind": "grid",
                        "label": "",
                        "columns": ["Model", "Use", "Thinking", "Capability", "Note"],
                        "rows": rows,
                    },
                    {
                        "name": "max_concurrency",
                        "label": "Pool max concurrency",
                        "kind": "number",
                        "value": (offered["pool"] or {}).get("max_concurrency") or 4,
                        "required": True,
                    },
                    {"name": "reason", "label": "Reason", "required": True},
                ],
            }
        sections.append(listing)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/gateway",
        heading="Local gateway",
        intro="The gateway Hermes uses: its URL, its key, a test of both, and its models.",
        sections=sections,
        badge="tested" if view["last_outcome"] == "probe:completed" else "not verified",
        badge_kind="ok" if view["last_outcome"] == "probe:completed" else "warn",
    )


@router.get("/images", response_class=HTMLResponse)
async def images_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    items = await images.list_all(ctx.admin, uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "Worker images",
            # One worker image carries all four harnesses (C11): the row lists the
            # version of each, and promoting it switches (or rolls back) all four.
            "columns": ["Harnesses", "Reference", "Digest", "Supported", "Promotion"],
            "rows": [
                [
                    ", ".join(
                        f"{name} {version}"
                        for name, version in sorted((item.get("harnesses") or {}).items())
                    ),
                    item.get("reference"),
                    item.get("digest"),
                    "yes" if item.get("supported") else "no",
                    item.get("promotion_state"),
                ]
                for item in items
            ],
        }
    ]
    if principal.role is Role.ADMIN:
        sections.append(
            {
                "title": "Promote an image",
                "form": {
                    "action": "/ui/actions/image-promote",
                    "label": "Promote",
                    "fields": [
                        {"name": "digest", "label": "Digest or reference", "required": True},
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/images",
        heading="Images",
        intro=(
            "Images visible to providers and the explicit default per harness. "
            "CI proof tags (ci-*) are not resolved or listed."
        ),
        sections=sections,
    )


@router.get("/routing", response_class=HTMLResponse)
def routing_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    versions = list(uow.policies.list_versions("default-software"))
    policy = max(versions, key=lambda item: item.version) if versions else None
    routing_ref = ((policy.document.get("routing") or {}).get("policy") or {}) if policy else {}
    routing_record = (
        uow.routing_policies.get(
            str(routing_ref.get("name", "")), int(routing_ref.get("version", 0))
        )
        if routing_ref
        else None
    )
    assert ctx.admin is not None
    exhaustion = routing.list_exhaustions(ctx.admin, uow)
    local = routing.local_endpoint_view(uow)
    egress = kubernetes_admin.egress_view(ctx.admin, uow)
    gateway_endpoint, _source = routing.gateway_url(uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "Local gateway",
            "note": (
                "The gateway URL, the Hermes key, the test of both, and which of the "
                "gateway's models to use are set in one place (crucible#119, #121)."
            ),
            "columns": ["Gateway URL", "Local models enabled", "Set up"],
            "rows": [
                [
                    gateway_endpoint or "not set",
                    ", ".join(m["id"] for m in local["models"] if m.get("enabled")) or "none",
                    "/ui/gateway",
                ]
            ],
        },
        _document_section("Local endpoint", local),
        _document_section("Kubernetes egress selectors", egress),
        _document_section("Active policy", policy.document if policy else {}),
        _document_section("Routing policy", routing_record.document if routing_record else {}),
        _document_section("Pool exhaustion", exhaustion),
    ]
    if principal.role is Role.ADMIN:
        dns = egress["document"].get("dns") or {}
        endpoint = egress["document"].get("local_endpoint") or {}
        sections.append(
            {
                "title": "Edit Kubernetes egress selectors",
                "note": (
                    "How a Kubernetes worker reaches cluster DNS and an in-cluster local "
                    "endpoint when the CNI translates service addresses before it applies "
                    "policy (Cilium with kube-proxy replacement). Labels are key=value, "
                    "comma separated. Leave the endpoint namespace empty for an endpoint "
                    "outside the cluster. Every process picks a save up within 15 seconds "
                    "and re-runs the namespace readiness canary before a launch uses it."
                ),
                "form": {
                    "action": "/ui/actions/kubernetes-egress",
                    "label": "Save egress selectors",
                    "fields": [
                        {
                            "name": "dns_namespace",
                            "label": "DNS namespace",
                            "value": dns.get("namespace", ""),
                        },
                        {
                            "name": "dns_labels",
                            "label": "DNS pod labels",
                            "value": format_labels(dns.get("pod_labels") or {}),
                        },
                        {
                            "name": "endpoint_namespace",
                            "label": "Local endpoint namespace",
                            "value": endpoint.get("namespace", ""),
                        },
                        {
                            "name": "endpoint_labels",
                            "label": "Local endpoint pod labels",
                            "value": format_labels(endpoint.get("pod_labels") or {}),
                        },
                        {
                            "name": "endpoint_port",
                            "label": "Local endpoint pod port (0: the URL's port)",
                            "kind": "number",
                            "value": endpoint.get("port", 0),
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.extend(
            [
                {
                    "title": "Upload routing policy version",
                    "note": (
                        "Upload a complete validated JSON document. Referenced versions "
                        "remain immutable."
                    ),
                    "form": {
                        "action": "/ui/actions/routing-upload",
                        "label": "Upload routing",
                        "fields": [
                            {
                                "name": "name",
                                "label": "Name",
                                "value": "default-routing",
                                "required": True,
                            },
                            {
                                "name": "version",
                                "label": "Version",
                                "kind": "number",
                                "required": True,
                            },
                            {
                                "name": "document",
                                "label": "Document",
                                "kind": "textarea",
                                "rows": 12,
                                "required": True,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
                {
                    "title": "Upload delivery policy version",
                    "form": {
                        "action": "/ui/actions/policy-upload",
                        "label": "Upload policy",
                        "fields": [
                            {
                                "name": "name",
                                "label": "Name",
                                "value": "default-software",
                                "required": True,
                            },
                            {
                                "name": "version",
                                "label": "Version",
                                "kind": "number",
                                "required": True,
                            },
                            {
                                "name": "document",
                                "label": "Document",
                                "kind": "textarea",
                                "rows": 12,
                                "required": True,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
                {
                    "title": "Clear pool exhaustion",
                    "form": {
                        "action": "/ui/actions/routing-clear",
                        "label": "Clear mark",
                        "fields": [
                            {"name": "pool", "label": "Pool", "required": True},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/routing",
        heading="Routing",
        intro="Policy versions, pools, model roster, limits, and exhaustion marks.",
        sections=sections,
    )


@router.get("/repositories", response_class=HTMLResponse)
def repositories_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = list(uow.repositories.list_all())
    sections: list[dict[str, Any]] = [
        {
            "title": "Registered repositories",
            "columns": [
                "Name",
                "URL",
                "Default branch",
                "Policy",
                "Installation",
                "External review",
            ],
            "rows": [
                [
                    item.name,
                    item.url,
                    item.default_branch,
                    item.policy_name,
                    item.installation_id,
                    item.external_review_attested,
                ]
                for item in items
            ],
        }
    ]
    if principal.role is Role.ADMIN:
        sections.extend(
            [
                {
                    "title": "Register or update",
                    "note": (
                        "For a repository the GitHub page's picker cannot show. The picker "
                        "fills the installation ID and default branch from GitHub."
                    ),
                    "form": {
                        "action": "/ui/actions/repository-register",
                        "label": "Save registration",
                        "fields": [
                            {"name": "name", "label": "Name", "required": True},
                            {"name": "url", "label": "Clone URL", "required": True},
                            {
                                "name": "default_branch",
                                "label": "Default branch",
                                "value": "main",
                                "required": True,
                            },
                            {
                                "name": "policy_name",
                                "label": "Policy",
                                "value": "default-software",
                                "required": True,
                            },
                            {
                                "name": "installation_id",
                                "label": "GitHub installation ID",
                                "kind": "number",
                            },
                            {
                                "name": "attested_all_prs",
                                "label": "External reviewer covers all PRs",
                                "kind": "checkbox",
                            },
                            {"name": "attested_by", "label": "Attested by"},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
                {
                    "title": "Remove unreferenced registration",
                    "form": {
                        "action": "/ui/actions/repository-remove",
                        "label": "Remove",
                        "danger": True,
                        "fields": [
                            {"name": "name", "label": "Name", "required": True},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/repositories",
        heading="Repositories",
        intro="Delivery registrations and GitHub installation binding.",
        sections=sections,
    )


@router.get("/tokens", response_class=HTMLResponse)
def tokens_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = tokens.list_principals(uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "Principals",
            "columns": ["ID", "Name", "Role", "Created", "Revoked"],
            "rows": [
                [item["id"], item["name"], item["role"], item["created_at"], item["disabled_at"]]
                for item in items
            ],
        }
    ]
    if principal.role is Role.ADMIN:
        sections.extend(
            [
                {
                    "title": "Create token",
                    "note": (
                        "The token is shown on the next page once and is never stored in plaintext."
                    ),
                    "form": {
                        "action": "/ui/actions/token-create",
                        "label": "Create token",
                        "fields": [
                            {"name": "name", "label": "Principal name", "required": True},
                            {
                                "name": "role",
                                "label": "Role",
                                "kind": "select",
                                "options": [(role.value, role.value) for role in Role],
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
                {
                    "title": "Revoke token",
                    "form": {
                        "action": "/ui/actions/token-revoke",
                        "label": "Revoke",
                        "danger": True,
                        "fields": [
                            {"name": "principal_id", "label": "Principal ID", "required": True},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tokens",
        heading="Tokens",
        intro="Principals share the same bearer credentials across API, CLI, and UI.",
        sections=sections,
    )


@router.get("/github", response_class=HTMLResponse)
def github_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """crucible#120: connect an existing App by its id and key, install it from its own
    link, then pick repositories from what each installation covers."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    state = github.status(ctx.admin, uow)
    picker = github.apps_view(ctx.admin, uow) if state["configured"] else None
    sections: list[dict[str, Any]] = [_document_section("App and repository connectivity", state)]
    admin = principal.role is Role.ADMIN
    if admin:
        sections.append(
            {
                "title": "Connect a GitHub App" if not state["configured"] else "Replace the App",
                "note": (
                    "An existing App's numeric ID and one of its private keys (the whole "
                    ".pem file). GitHub is asked about them before anything is stored; the "
                    "service then keeps them (on Kubernetes the Secret crucible-github-app "
                    "in its own namespace, which it alone writes). The key is never shown or "
                    "audited; its public fingerprint is."
                ),
                "form": {
                    "action": "/ui/actions/github-connect",
                    "label": "Check and connect",
                    "fields": [
                        {
                            "name": "app_id",
                            "label": "App ID",
                            "kind": "number",
                            "value": state["app_id"] or "",
                            "required": True,
                        },
                        {
                            "name": "private_key",
                            "label": "Private key (.pem)",
                            "kind": "textarea",
                            "rows": 6,
                            "required": True,
                        },
                        {
                            "name": "webhook_secret",
                            "label": "Webhook secret (optional)",
                            "kind": "password",
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
    if picker is not None:
        if picker["error"]:
            sections.append({"title": "Installations", "note": picker["error"]})
        if picker["install_url"]:
            sections.append(
                {
                    "title": "Install the App",
                    "note": (
                        "Install it on each account or organization whose repositories "
                        "Crucible should deliver to, then come back here."
                    ),
                    "columns": ["App", "Install link"],
                    "rows": [
                        [
                            (picker["app"] or {}).get("name") or (picker["app"] or {}).get("slug"),
                            {"href": picker["install_url"], "label": picker["install_url"]},
                        ]
                    ],
                }
            )
        for installation in picker["installations"]:
            title = (
                f"{installation.get('account')} ({installation.get('account_type') or 'account'}), "
                f"installation {installation['id']}"
            )
            repositories = installation["repositories"]
            section: dict[str, Any] = {
                "title": title,
                "note": installation["error"]
                or f"{len(repositories)} repositor{'y' if len(repositories) == 1 else 'ies'} "
                "this installation covers.",
                "columns": ["Repository", "Default branch", "Private", "Archived", "Registered as"],
                "rows": [
                    [
                        repo["full_name"],
                        repo["default_branch"],
                        repo.get("unsupported") or repo["private"],
                        repo["archived"],
                        repo["registered_as"] or "not registered",
                    ]
                    for repo in repositories
                ],
            }
            choices = [
                (repo["full_name"], repo["full_name"])
                for repo in repositories
                if not (repo["archived"] or repo["registered_as"] or repo.get("unsupported"))
            ]
            if admin and choices:
                section["form"] = {
                    "action": "/ui/actions/github-add-repository",
                    "label": "Register repository",
                    "fields": [
                        {"name": "installation_id", "kind": "hidden", "value": installation["id"]},
                        {
                            "name": "repository",
                            "label": "Repository",
                            "kind": "select",
                            "options": choices,
                        },
                        {
                            "name": "name",
                            "label": "Registered name (empty: the repository's own)",
                        },
                        {
                            "name": "policy_name",
                            "label": "Policy",
                            "value": "default-software",
                            "required": True,
                        },
                        {
                            "name": "attested_all_prs",
                            "label": "External reviewer covers all PRs",
                            "kind": "checkbox",
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                }
            sections.append(section)
        sections.append(
            {
                "title": "A repository the picker cannot show",
                "note": "Register it by hand on Repositories.",
                "columns": ["Page", "Open"],
                "rows": [["Repositories", "/ui/repositories"]],
            }
        )
    if admin and state["configured"]:
        sections.append(
            {
                "title": "Connectivity check",
                "form": {
                    "action": "/ui/actions/github-check",
                    "label": "Check every repository",
                    "fields": [{"name": "reason", "label": "Reason", "required": True}],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/github",
        heading="GitHub",
        intro="Connect the App, install it, and pick the repositories Crucible delivers to.",
        sections=sections,
        badge="connected" if state["configured"] else "not connected",
        badge_kind="ok" if state["configured"] else "warn",
    )


@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = status.workers(uow)
    sections = [
        {
            "title": "Active attempts",
            "columns": [
                "Attempt",
                "State",
                "Task",
                "External ID",
                "Harness",
                "Model",
                "Image",
                "Started",
                "Heartbeat",
            ],
            "rows": [
                [
                    item.get("attempt_id"),
                    item.get("state"),
                    item.get("task_id"),
                    item.get("external_id"),
                    item.get("harness"),
                    item.get("model"),
                    item.get("image_digest"),
                    item.get("started_at"),
                    item.get("last_heartbeat"),
                ]
                for item in rows
            ],
        }
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading="Active workers",
        intro="Current attempts. Select an attempt log below by entering its ID.",
        sections=[
            *sections,
            {
                "title": "Live log tail",
                "form": {
                    "action": "/ui/actions/log-tail",
                    "label": "Open log",
                    "fields": [{"name": "attempt_id", "label": "Attempt ID", "required": True}],
                },
            },
        ],
    )


@router.get("/workers/{attempt_id}/logs", response_class=HTMLResponse)
def worker_logs(request: Request, attempt_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    chunks = uow.logs.list_from_offset(
        attempt_id, offset=max(0, uow.logs.last_offset(attempt_id) - 65536)
    )
    text = b"".join(chunk.content for chunk in chunks).decode("utf-8", "replace")
    return _page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading=f"Log tail: {attempt_id}",
        intro="Stored stdout and stderr tail. Refresh to follow an active attempt.",
        sections=[{"title": "Tail", "text": redact(text[-65536:])}],
    )


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tasks",
        heading="Failed and blocked tasks",
        intro="States that need operator attention, plus counts across the lifecycle.",
        sections=[_document_section("Task state", status.tasks(uow))],
    )


@router.get("/wakes", response_class=HTMLResponse)
def wakes_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = uow.wakes.list_for_principal(principal.id, since=None, include_acked=True, limit=200)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/wakes",
        heading="Pending wakes",
        intro="Durable operator notifications for the signed-in principal.",
        sections=[
            _document_section("Wakes", status.wakes(uow)),
            {
                "title": "Your wake records",
                "columns": ["ID", "Reason", "Created", "Acknowledged", "Payload"],
                "rows": [
                    [
                        item.id,
                        item.reason,
                        item.created_at.isoformat(),
                        item.acked_at.isoformat() if item.acked_at else None,
                        item.payload,
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
    return _page(
        request,
        principal,
        csrf,
        active="/ui/retention",
        heading="Retention and cleanup",
        intro="Recent cleanup actions and the last observed sweep.",
        sections=[
            _document_section("Summary", status.retention(uow)),
            {
                "title": "Recent actions",
                "columns": ["Kind", "Subject", "Policy", "Time", "Detail"],
                "rows": [
                    [
                        item.kind,
                        item.subject,
                        f"{item.policy_name}/{item.policy_version}",
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
    return _page(
        request,
        principal,
        csrf,
        active="/ui/audit",
        heading="Administrative audit",
        intro="Cursor-paged changes, refusals, principals, and reasons.",
        sections=[
            {
                "title": f"Events after {cursor}",
                "columns": ["Sequence", "Time", "Kind", "Principal", "Payload"],
                "rows": [
                    [item["seq"], item["ts"], item["kind"], item["principal"], item["payload"]]
                    for item in document["items"]
                ],
            },
            _document_section("Next cursor", {"next_cursor": document["next_cursor"]}),
        ],
    )


@router.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = bootstrap.list_imports(uow)
    sections: list[dict[str, Any]] = [
        _document_section("Imports", items),
        {
            "title": "Show import",
            "form": {
                "action": "/ui/actions/bootstrap-show",
                "label": "Show",
                "fields": [{"name": "import_id", "label": "Import ID", "required": True}],
            },
        },
    ]
    if principal.role is Role.ADMIN:
        sections.append(
            {
                "title": "Commit verified import",
                "form": {
                    "action": "/ui/actions/bootstrap-commit",
                    "label": "Commit import",
                    "fields": [
                        {"name": "import_id", "label": "Import ID", "required": True},
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        heading="Bootstrap imports",
        intro="Verified imports, manifests, and the explicit authoritative commit.",
        sections=sections,
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
            "Read at process start; restart required. Secret value is never shown."
            if path in sensitive
            else "Seeds kubernetes.egress; edit it on Routing, where a saved value wins."
            if path.split(".")[:2] in _EGRESS_SEEDS
            else "Read at process start; restart required."
        )
        rows.append([path, shown, source, reason])
    return rows


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    return _page(
        request,
        principal,
        csrf,
        active="/ui/settings",
        heading="Restart-bound settings",
        intro=(
            "Effective process configuration, its source, and why it is read-only here. "
            "Runtime policy knobs are on Routing."
        ),
        sections=[
            {
                "title": "Effective settings",
                "columns": ["Setting", "Effective value", "Source", "Disposition"],
                "rows": _settings_rows(ctx.settings),
            }
        ],
    )


@router.post("/actions/{action}")
async def action(request: Request, action: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        if action == "log-tail":
            return RedirectResponse(
                f"/ui/workers/{quote(form.get('attempt_id', ''))}/logs", status_code=303
            )
        if action == "bootstrap-show":
            document = bootstrap.show(uow, form.get("import_id", ""))
            return _page(
                request,
                principal,
                csrf,
                active="/ui/bootstrap",
                heading="Bootstrap manifest",
                intro=form.get("import_id", ""),
                sections=[_document_section("Manifest", document)],
            )
        _admin(principal)
        if ctx.admin is None:
            raise ConflictError("the administrative surface is not configured")
        reason = form.get("reason")
        if action == "harness":
            harnesses.set_enabled(
                ctx.admin,
                uow,
                principal=principal.name,
                harness=form.get("harness", ""),
                enabled=form.get("enabled") == "true",
                reason=reason,
            )
        elif action == "credential":
            verb = form.get("verb")
            if verb == "validate":
                await credentials.validate(
                    ctx.admin,
                    uow,
                    principal=principal.name,
                    harness=form.get("harness", ""),
                    reason=reason,
                )
            elif verb == "probe":
                await credentials.probe(
                    ctx.admin,
                    uow,
                    principal=principal.name,
                    harness=form.get("harness", ""),
                    reason=reason,
                )
            elif verb == "remove":
                credentials.remove(
                    ctx.admin,
                    uow,
                    principal=principal.name,
                    harness=form.get("harness", ""),
                    reason=reason,
                )
            elif verb == "rotate":
                credentials.rotate(
                    ctx.admin,
                    uow,
                    principal=principal.name,
                    harness=form.get("harness", ""),
                    new_path=form.get("new_path", ""),
                    reason=reason,
                )
            else:
                raise ConflictError("unknown credential action")
        elif action == "gateway-save":
            result = await gateway.save_gateway(
                ctx.admin,
                uow,
                principal=principal,
                endpoint_url=form.get("endpoint_url", ""),
                api_key=form.get("api_key") or None,
                reason=reason,
            )
            uow.commit()
            return _redirect(
                form,
                f"Saved. {result['test']['summary']}",
                kind="ok" if result["test"]["passed"] else "warn",
            )
        elif action == "gateway-test":
            result = await gateway.test_gateway(ctx.admin, uow, principal=principal, reason=reason)
            uow.commit()
            return _redirect(
                form,
                result["test"]["summary"],
                kind="ok" if result["test"]["passed"] else "warn",
            )
        elif action == "gateway-models":
            picks = []
            index = 0
            while f"model.{index}.id" in form:
                picks.append(
                    {
                        "id": form[f"model.{index}.id"],
                        "enabled": form.get(f"model.{index}.enabled") == "true",
                        "enable_thinking": form.get(f"model.{index}.thinking") == "true",
                        "capability": form.get(f"model.{index}.capability") or None,
                    }
                )
                index += 1
            saved = await gateway.save_models(
                ctx.admin,
                uow,
                principal=principal,
                models=picks,
                max_concurrency=int(form.get("max_concurrency") or "0") or None,
                reason=reason,
            )
            uow.commit()
            enabled = ", ".join(saved["enabled"]) or "none"
            dropped = saved["disabled_not_offered"]
            return _redirect(
                form,
                f"Saved routing policy version {saved['routing_policy']['version']}. "
                f"Enabled: {enabled}."
                + (f" Disabled as no longer offered: {', '.join(dropped)}." if dropped else ""),
            )
        elif action == "github-connect":
            connected = github.connect(
                ctx.admin,
                uow,
                principal=principal.name,
                app_id=int(form.get("app_id") or "0"),
                private_key=form.get("private_key", ""),
                webhook_secret=form.get("webhook_secret") or None,
                reason=reason,
            )
            uow.commit()
            return _redirect(
                form,
                f"Connected App {connected['app_id']}"
                + (f" ({connected['app'].get('slug')})" if connected["app"].get("slug") else "")
                + ". Install it from the link below, then pick repositories.",
            )
        elif action == "github-add-repository":
            added = github.add_repository(
                ctx.admin,
                uow,
                principal=principal.name,
                installation_id=int(form.get("installation_id") or "0"),
                repository=form.get("repository", ""),
                name=form.get("name") or None,
                policy_name=form.get("policy_name") or "default-software",
                attested_all_prs=form.get("attested_all_prs") == "true",
                attested_by=None,
                reason=reason,
            )
            uow.commit()
            return _redirect(
                form,
                f"Registered {added['repository']} ({added['url']}, default branch "
                f"{added['default_branch']}, installation {added['installation_id']}).",
            )
        elif action == "login-start":
            login.start_login(
                ctx.admin,
                uow,
                ctx.logins,
                principal=principal.name,
                harness=form.get("harness", ""),
                reason=reason,
                replace=form.get("replace") == "true",
            )
        elif action == "login-code":
            login.submit_code(
                ctx.logins,
                form.get("harness", ""),
                form.get("code", ""),
                ctx=ctx.admin,
                uow=uow,
                principal=principal.name,
                reason=reason,
            )
        elif action == "login-cancel":
            login.cancel_login(
                ctx.logins,
                form.get("harness", ""),
                ctx=ctx.admin,
                uow=uow,
                principal=principal.name,
                reason=reason,
            )
        elif action == "login-finish":
            login.finish_login(
                ctx.admin,
                uow,
                ctx.logins,
                principal=principal.name,
                harness=form.get("harness", ""),
                reason=reason,
            )
        elif action == "image-promote":
            await images.promote(
                ctx.admin,
                uow,
                principal=principal.name,
                digest=form.get("digest", ""),
                reason=reason,
            )
        elif action == "routing-clear":
            routing.clear_exhaustion(
                ctx.admin, uow, principal=principal.name, pool=form.get("pool", ""), reason=reason
            )
        elif action == "kubernetes-egress":
            kubernetes_admin.save_egress(
                ctx.admin,
                uow,
                principal=principal.name,
                document={
                    "dns": {
                        "namespace": form.get("dns_namespace", ""),
                        "pod_labels": parse_labels(form.get("dns_labels", "")),
                    },
                    "local_endpoint": {
                        "namespace": form.get("endpoint_namespace", ""),
                        "pod_labels": parse_labels(form.get("endpoint_labels", "")),
                        "port": int(form.get("endpoint_port") or "0"),
                    },
                },
                reason=reason,
            )
        elif action in ("routing-upload", "policy-upload"):
            raw = form.get("document", "")
            if scan_text(raw) is not None:
                raise ConflictError("the policy document looks like it contains a secret")
            document = json.loads(raw)
            audited_reason = guard_mutation(
                ctx.admin,
                uow,
                reason,
                principal=principal.name,
                operation=action,
            )
            if action == "routing-upload":
                put_routing_policy(
                    uow,
                    ctx.clock,
                    principal=principal,
                    name=form.get("name", ""),
                    version=int(form.get("version", "0")),
                    document=document,
                    reason=audited_reason,
                )
            else:
                put_policy(
                    uow,
                    ctx.clock,
                    principal=principal,
                    name=form.get("name", ""),
                    version=int(form.get("version", "0")),
                    document=document,
                    reason=audited_reason,
                )
        elif action == "repository-register":
            repositories.register(
                ctx.admin,
                uow,
                principal=principal.name,
                name=form.get("name", ""),
                registration=RepositoryRegistration(
                    url=form.get("url", ""),
                    default_branch=form.get("default_branch", "main"),
                    policy_name=form.get("policy_name", "default-software"),
                    installation_id=int(form["installation_id"])
                    if form.get("installation_id")
                    else None,
                    external_review=ExternalReviewAttestation(
                        attested_all_prs=form.get("attested_all_prs") == "true",
                        attested_by=form.get("attested_by") or None,
                    ),
                ),
                reason=reason,
            )
        elif action == "repository-remove":
            repositories.remove(
                ctx.admin, uow, principal=principal.name, name=form.get("name", ""), reason=reason
            )
        elif action == "token-create":
            minted = tokens.create(
                ctx.admin,
                uow,
                principal=principal.name,
                name=form.get("name", ""),
                role=form.get("role", "observer"),
                reason=reason,
            )
            uow.commit()
            context = _base(request, principal, csrf, title="Token created", active="/ui/tokens")
            context.update(
                token=minted.token,
                token_name=minted.principal.name,
                token_role=minted.principal.role.value,
            )
            response = templates.TemplateResponse(
                request=request, name="token_once.html", context=context
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        elif action == "token-revoke":
            revoked = tokens.revoke(
                ctx.admin,
                uow,
                principal=principal.name,
                principal_id=form.get("principal_id", ""),
                reason=reason,
            )
            uow.commit()
            await asyncio.to_thread(tokens.after_revoke, ctx.admin, revoked)
        elif action == "github-check":
            github.check(ctx.admin, uow, principal=principal.name, reason=reason)
        elif action == "bootstrap-commit":
            bootstrap.commit(
                ctx.admin,
                uow,
                principal=principal.name,
                import_id=form.get("import_id", ""),
                reason=reason,
            )
        else:
            raise ConflictError(f"unknown UI action {action!r}")
        uow.commit()
        return _redirect(form, f"Completed: {action}.")
    except (ApplicationError, ValueError, json.JSONDecodeError) as exc:
        detail = exc.detail if isinstance(exc, ApplicationError) else str(exc)
        return _redirect(form, detail, kind="bad")
