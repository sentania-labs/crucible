"""HTML administration adapter under ``/ui``."""

from __future__ import annotations

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
    github,
    harness_test,
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
from crucible.application.policies import put_policy, put_routing_policy
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.contracts.task_contract import HarnessName
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
# Grouped so the operator's path reads in order (crucible#115): what to set up, the work
# running, then administration. An entry with no link is a group's label.
NAV = (
    ("/ui", "Status"),
    ("", "Set up"),
    ("/ui/harnesses", "Harnesses"),
    ("/ui/images", "Images"),
    ("/ui/credentials", "Credentials"),
    ("/ui/routing", "Routing"),
    ("/ui/repositories", "Repositories"),
    ("/ui/github", "GitHub"),
    ("", "Work"),
    ("/ui/tasks", "Tasks"),
    ("/ui/workers", "Workers"),
    ("/ui/wakes", "Wakes"),
    ("", "Admin"),
    ("/ui/tokens", "Tokens"),
    ("/ui/audit", "Audit"),
    ("/ui/settings", "Settings"),
    ("/ui/retention", "Retention"),
    ("/ui/bootstrap", "Bootstrap"),
)
# Shown only once they have something in them, or while one is open: a new deployment
# has run no cleanup and imported no ledger (crucible#115).
HIDDEN_WHEN_EMPTY = ("/ui/retention", "/ui/bootstrap")


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

# A field is hidden when its own name says it holds a credential value (crucible#126).
# The name is compared whole, or by a credential suffix, never as a substring: a harness
# called `claude_code` is a harness, not a login code, and its version is not a secret.
SECRET_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth_code",
    "authorization",
    "authorization_code",
    "bearer",
    "client_secret",
    "code",
    "cookie",
    "credential_value",
    "device_code",
    "id_token",
    "oauth_token",
    "passwd",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
    "token",
    "user_code",
}
SECRET_SUFFIXES = ("_api_key", "_password", "_private_key", "_secret", "_token")
# Names that describe a credential without holding one: whether it is there, what it
# fingerprints to, where it is kept, when it changed.
DESCRIBES_SECRET_SUFFIXES = ("_at", "_fingerprint", "_path", "_present", "_set", "_source")
NON_SECRET_FIELDS = {
    "fenced_token",
    "tokens_in",
    "tokens_out",
    # Harness names key the version maps an image promotion records (crucible#126).
    *(harness.value.replace("-", "_") for harness in HarnessName),
}


# A reason is an audit note the operator may leave out (the operator's decision of
# 2026-09-25, crucible#117). These forms' services require one, because what they do is
# destructive or hard to reverse; a read-only check never asks for one.
REASON_REQUIRED_ACTIONS = frozenset(
    {
        "/ui/actions/bootstrap-commit",
        "/ui/actions/repository-remove",
        "/ui/actions/token-revoke",
    }
)
NO_REASON_ACTIONS = frozenset({"/ui/actions/github-check", "/ui/actions/harness-test"})


def _reason_fields(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every form's reason field, set by one rule rather than form by form: required
    where the service requires one, optional elsewhere, absent on a read-only check."""
    for section in sections:
        form = section.get("form")
        if not isinstance(form, dict):
            continue
        action = str(form.get("action", ""))
        fields = []
        for field in form.get("fields", []):
            if field.get("name") != "reason":
                fields.append(field)
                continue
            if action in NO_REASON_ACTIONS:
                continue
            required = action in REASON_REQUIRED_ACTIONS
            label = field.get("reason_label") or ("Reason" if required else "Reason (optional)")
            fields.append({**field, "label": label, "required": required})
        form["fields"] = fields
    return sections


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
    """Whether a field's value is a credential, decided by what its name means."""
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    name = separated.rsplit(".", 1)[-1].replace("-", "_")
    if name in NON_SECRET_FIELDS or name.endswith(DESCRIBES_SECRET_SUFFIXES):
        return False
    return name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES)


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
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "active": active,
        "nav": tuple(item for item in NAV if item[0] not in hidden or item[0] == active),
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
    sections = _reason_fields(_localize(sections, timezone))
    context = _base(
        request, principal, csrf, title=heading, active=active, hidden=_empty_sections(request)
    )
    context.update(
        heading=heading,
        intro=intro,
        sections=sections,
        badge=badge,
        badge_kind=badge_kind,
    )
    return templates.TemplateResponse(request=request, name="page.html", context=context)


def _empty_sections(request: Request) -> frozenset[str]:
    """The navigation entries with nothing behind them yet (HIDDEN_WHEN_EMPTY)."""
    try:
        factory = request.app.state.ctx.uow_factory
    except (AttributeError, KeyError):
        return frozenset()
    empty: set[str] = set()
    with factory() as uow:
        if not list(uow.retention.list_recent(1)):
            empty.add("/ui/retention")
        if not list(uow.bootstrap_imports.list_all()):
            empty.add("/ui/bootstrap")
    return frozenset(empty)


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


def _readiness_gaps(document: dict[str, Any], *, repository_registered: bool) -> list[list[str]]:
    """Plain operator actions derived only from fields in the status document."""
    gaps: list[list[str]] = []
    supervisor = document["supervisor"]
    if not supervisor["healthy"]:
        gaps.append([f"The supervisor is not ready: {supervisor['health_detail']}.", "/ui"])
    for item in document["harnesses"]:
        if item["enabled_by_configuration"] and not item["enabled"]:
            gaps.append(
                [f"{item['name']} is disabled. Open Harnesses to enable it.", "/ui/harnesses"]
            )
        if item["credential"]["state"] in ("absent", "invalid"):
            gaps.append(
                [
                    f"{item['name']} needs a credential. Open Credentials to log in.",
                    "/ui/credentials",
                ]
            )
        if item["enabled"] and not item["default_image"]:
            gaps.append(
                [
                    f"{item['name']} has no promoted worker image. Open Images to choose one.",
                    "/ui/images",
                ]
            )
    if not repository_registered:
        gaps.append(
            [
                "No repository is registered. Open Repositories before submitting work.",
                "/ui/repositories",
            ]
        )
    return gaps


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
    context.update(next=next_path, csrf=csrf, message=message, message_kind="bad")
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


# Task states in the words an operator uses (crucible#115). A state not named here is
# shown as its own name with the underscores taken out.
STATE_WORDS = {
    "blocked": "Blocked: needs a decision",
    "pre_pr_gates_failed": "Checks failed before the pull request",
    "publish_failed": "Publishing failed",
    "ci_certification_failed": "CI did not certify",
    "head_diverged": "Branch changed outside Crucible",
    "awaiting_internal_review": "Awaiting internal review",
    "awaiting_acceptance": "Awaiting acceptance",
    "awaiting_external_review": "Awaiting external review",
    "awaiting_ci_certification": "Awaiting CI",
    "ready_for_merge": "Ready to merge",
}
PROVIDER_TONES = {"ok": "ok", "degraded": "warn", "unavailable": "bad"}


def _state_words(state: str) -> str:
    return STATE_WORDS.get(state, state.replace("_", " ").capitalize())


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
    gaps = _readiness_gaps(document, repository_registered=bool(uow.repositories.list_all()))
    supervisor = document["supervisor"]
    tasks_part = document["tasks"]
    attention = sum(len(rows) for rows in tasks_part["lists"].values())
    running = len(document["workers"])
    sections: list[dict[str, Any]] = []
    if gaps:
        sections.append(
            {
                "title": "Before a task can run",
                "rows": gaps,
                "columns": ["Action", "Fix page"],
            }
        )
    overview: list[list[Any]] = [
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
                {"title": "Supervisor", "panel": _panel(internals)},
                *[
                    {"title": f"Provider {item['name']} checks", "panel": _panel(item["checks"])}
                    for item in document["providers"]
                ],
            ],
        }
    )
    ready = not gaps and supervisor["healthy"]
    return _page(
        request,
        principal,
        csrf,
        active="/ui",
        heading="Status",
        intro="Whether Crucible can run a task now, and what needs you.",
        sections=sections,
        badge="ready" if ready else "attention needed",
        badge_kind="ok" if ready else "warn",
    )


def _harness_status(item: dict[str, Any]) -> dict[str, Any]:
    """The one word an operator acts on, most blocking first."""
    if not item["enabled_by_configuration"]:
        return {"kind": "status", "value": "off in configuration", "tone": "bad"}
    if not item["enabled"]:
        return {"kind": "status", "value": "disabled", "tone": "warn"}
    if not item.get("default_image"):
        return {"kind": "status", "value": "needs an image", "tone": "warn"}
    if item["credential"]["state"] in ("absent", "invalid"):
        return {"kind": "status", "value": "needs a credential", "tone": "warn"}
    return {"kind": "status", "value": "ready", "tone": "ok"}


def _test_cell(last: dict[str, Any] | None) -> dict[str, Any]:
    if not last:
        return {"kind": "note", "value": "not tested yet"}
    tones = {"pass": "ok", "fail": "bad", "not run": "accent"}
    return {
        "kind": "steps",
        "items": [
            {**step, "tone": tones.get(str(step.get("result")), "accent")}
            for step in last.get("steps", [])
            if step.get("result") != "not run"
        ],
        "tested_at": last.get("tested_at"),
    }


@router.get("/harnesses", response_class=HTMLResponse)
async def harness_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    discovered = await harnesses.list_images(ctx.admin)
    items = harnesses.list_harnesses(ctx.admin, uow, [item for _, item in discovered])
    admin = principal.role is Role.ADMIN
    rows: list[list[Any]] = []
    for item in items:
        name = item["name"]
        image = item.get("default_image")
        last = item.get("last_test")
        actions: list[dict[str, Any]] = []
        if admin and item["enabled_by_configuration"]:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/harness-test",
                    "label": "Test",
                    "primary": True,
                    "hidden": {"harness": name},
                }
            )
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/harness",
                    "label": "Disable" if item["enabled_by_administrator"] else "Enable",
                    "hidden": {
                        "harness": name,
                        "enabled": "false" if item["enabled_by_administrator"] else "true",
                    },
                }
            )
        rows.append(
            [
                name,
                _harness_status(item),
                (
                    {
                        "kind": "note",
                        "value": image["reference"],
                        "hint": f"{name} {image['version']}",
                    }
                    if image
                    else {"kind": "link", "href": "/ui/images", "label": "Choose on Images"}
                ),
                item["credential"]["state"].replace("_", " "),
                _test_cell(last),
                {"kind": "actions", "items": actions} if actions else "",
            ]
        )
    sections: list[dict[str, Any]] = [
        {
            "title": "Harnesses",
            "note": (
                "Test runs what a task runs: the harness's image, its credential, a worker "
                "under the worker's egress, and one small model call. It takes up to a "
                "couple of minutes."
            ),
            "columns": ["Harness", "Status", "Image", "Credential", "Last test", ""],
            "rows": rows,
            "details": [
                {
                    "title": "Gates, versions and use",
                    "columns": [
                        "Harness",
                        "Configuration gate",
                        "Runtime gate",
                        "Why",
                        "Tested versions",
                        "Running now",
                    ],
                    "rows": [
                        [
                            item["name"],
                            "on" if item["enabled_by_configuration"] else "off",
                            "on" if item["enabled_by_administrator"] else "off",
                            item["reason"] or "none",
                            item["supported_versions"],
                            item.get("concurrency_in_use", 0),
                        ]
                        for item in items
                    ],
                    "note": (
                        "The configuration gate is restart-bound (Settings); the runtime "
                        "gate is the Enable and Disable buttons above."
                    ),
                }
            ],
        }
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/harnesses",
        heading="Harnesses",
        intro="Whether each harness can run a task, and a test that proves it.",
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
    # Where the credentials are Secrets the service owns (Kubernetes, ADR 0015), rotate
    # and remove move and shred directories and are refused, so they are not offered
    # (crucible#125).
    secrets_held = credentials.secret_store(ctx.admin) is not None
    rows: list[list[Any]] = []
    for name in names:
        view = credentials.state_view(ctx.admin, uow, name)
        # A login exists only for a harness that logs in: not Hermes, which takes a key,
        # and not a harness that needs no credential (crucible#125).
        logs_in = name != "hermes" and view.get("state") != "not_required"
        rows.append(
            [
                name,
                view.get("state"),
                view.get("session_compatibility"),
                f"/ui/credentials/{name}/login" if logs_in else "none",
            ]
        )
    sections: list[dict[str, Any]] = [
        {
            "title": "Credential state",
            "columns": ["Harness", "State", "Compatibility", "Login page"],
            "rows": rows,
        }
    ]
    if principal.role is Role.ADMIN:
        options = [
            (name, name)
            for name in names
            if credentials.state_view(ctx.admin, uow, name).get("state") != "not_required"
        ]
        sections.append(
            {
                "title": "Set Hermes API key",
                "note": (
                    "The value is written to the Hermes credential (a file mode 0600, or "
                    "on Kubernetes the Secret Crucible owns), then discarded from the "
                    "request. It is never displayed or included in audit details."
                ),
                "form": {
                    "action": "/ui/actions/credential-set",
                    "label": "Set and probe",
                    "fields": [
                        {
                            "name": "api_key",
                            "label": "LiteLLM virtual key",
                            "kind": "password",
                            "required": True,
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
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
                                *(
                                    []
                                    if secrets_held
                                    else [
                                        ("rotate", "Rotate from prepared server directory"),
                                        ("remove", "Remove"),
                                    ]
                                ),
                            ],
                        },
                        {
                            "name": "reason",
                            "label": "Reason",
                            "reason_label": (
                                "Reason (optional)"
                                if secrets_held
                                else "Reason (required to remove)"
                            ),
                        },
                        *(
                            []
                            if secrets_held
                            else [
                                {
                                    "name": "new_path",
                                    "label": "Prepared directory (rotate only)",
                                }
                            ]
                        ),
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


def _image_label(entry: dict[str, Any] | None, harness: str) -> str:
    if not entry:
        return "none"
    return f"{entry['reference']} ({harness} {entry['version']})"


def _image_rows(rows: list[dict[str, Any]], *, admin: bool) -> list[list[Any]]:
    """One row per harness (ADR 0016): its default, the image a rollback returns to, and
    a pulldown of the images that carry it at a supported version."""
    out: list[list[Any]] = []
    for row in rows:
        harness = row["harness"]
        current = row.get("current")
        previous = row.get("previous")
        actions: list[dict[str, Any]] = []
        if admin and row["choices"]:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/image-promote",
                    "label": "Promote",
                    "primary": True,
                    "hidden": {"harness": harness},
                    "select": {
                        "name": "digest",
                        "label": f"Image for {harness}",
                        "options": [
                            (choice["digest"], f"{choice['reference']} ({choice['version']})")
                            for choice in row["choices"]
                        ],
                        "selected": (current or {}).get("digest"),
                    },
                }
            )
        if admin and previous:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/image-rollback",
                    "label": f"Roll back to {previous['reference']}",
                    "hidden": {"harness": harness},
                }
            )
        out.append(
            [
                harness,
                (
                    {
                        "kind": "note",
                        "value": current["reference"],
                        "hint": f"{harness} {current['version']}",
                    }
                    if current
                    else {"kind": "status", "value": "none promoted", "tone": "warn"}
                ),
                _image_label(previous, harness) if previous else "none",
                {"kind": "actions", "items": actions}
                if actions
                else {
                    "kind": "note",
                    "value": "No image to offer",
                    "hint": (
                        f"No provider sees a release image with {harness} "
                        f"{row['supported_versions']}"
                    ),
                },
            ]
        )
    return out


@router.get("/images", response_class=HTMLResponse)
async def images_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    rows = await images.defaults(ctx.admin, uow)
    items = await images.list_all(ctx.admin, uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "Worker image per harness",
            "note": (
                "Each harness runs its own default image. Promoting one moves only that "
                "harness; Roll back returns it to the image it had before."
            ),
            "columns": ["Harness", "Current image", "Previous image", "Change"],
            "rows": _image_rows(rows, admin=principal.role is Role.ADMIN),
            "details_label": "Every image the providers see",
            "details": [
                {
                    "title": "Images",
                    "columns": ["Reference", "Harnesses", "Default for", "Digest"],
                    "rows": [
                        [
                            item.get("reference"),
                            ", ".join(
                                f"{name} {version}"
                                for name, version in sorted((item.get("harnesses") or {}).items())
                            ),
                            ", ".join(item.get("default_for") or []) or "none",
                            item.get("digest"),
                        ]
                        for item in items
                    ],
                }
            ],
        }
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/images",
        heading="Images",
        intro="Which worker image each harness runs.",
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
    hermes: dict[str, Any] = {}
    if "hermes" in ctx.admin.harnesses.names():
        view = credentials.state_view(ctx.admin, uow, "hermes")
        # Whether a key is set and where it is kept; the key itself is never shown (12).
        hermes = {
            "key_set": view.get("key_set", False),
            "state": view.get("state"),
            "stored_in": view.get("source") or "the configured credential directory",
        }
    sections: list[dict[str, Any]] = [
        _document_section("Local endpoint", local),
        *([_document_section("Hermes API key", hermes)] if hermes else []),
        _document_section("Kubernetes egress selectors", egress),
        _document_section("Active policy", policy.document if policy else {}),
        _document_section("Routing policy", routing_record.document if routing_record else {}),
        _document_section("Pool exhaustion", exhaustion),
    ]
    if principal.role is Role.ADMIN:
        for model in local["models"]:
            sections.append(
                {
                    "title": f"Edit local model {model['id']}",
                    "note": (
                        "Saving creates new immutable routing and delivery policy versions "
                        "and regenerates the worker proxy allowlist."
                    ),
                    "form": {
                        "action": "/ui/actions/routing-local",
                        "label": "Save local endpoint",
                        "fields": [
                            {
                                "name": "endpoint_url",
                                "label": "Endpoint URL",
                                "kind": "url",
                                "value": local.get("endpoint_url") or "",
                                "required": True,
                            },
                            {
                                "name": "model_id",
                                "label": "Model",
                                "value": model["id"],
                                "required": True,
                            },
                            {
                                "name": "enabled",
                                "label": "Enabled",
                                "kind": "checkbox",
                                "value": model.get("enabled", False),
                            },
                            {
                                "name": "enable_thinking",
                                "label": "Thinking by default",
                                "kind": "checkbox",
                                "value": (model.get("chat_template_kwargs") or {}).get(
                                    "enable_thinking", False
                                ),
                            },
                            {
                                "name": "max_concurrency",
                                "label": "Pool max concurrency",
                                "kind": "number",
                                "value": local["pool"].get("max_concurrency", 1),
                                "required": True,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                }
            )
        if hermes:
            sections.append(
                {
                    "title": "Set Hermes API key",
                    "note": (
                        "The LiteLLM virtual key Hermes sends to the local endpoint. It is "
                        "written to the Hermes credential (a file mode 0600, or on Kubernetes "
                        "the Secret Crucible owns), then probed, and never displayed or "
                        "included in audit details."
                    ),
                    "form": {
                        "action": "/ui/actions/credential-set",
                        "label": "Set and probe",
                        "fields": [
                            {
                                "name": "api_key",
                                "label": "LiteLLM virtual key",
                                "kind": "password",
                                "required": True,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                }
            )
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
                "",
            ],
            "rows": [
                [
                    item.name,
                    item.url,
                    item.default_branch,
                    item.policy_name,
                    item.installation_id,
                    item.external_review_attested,
                    # The row's own removal, never a typed name (crucible#127); refused
                    # while tasks reference it, and it asks for a reason (crucible#117).
                    {
                        "kind": "form",
                        "action": "/ui/actions/repository-remove",
                        "label": "Remove",
                        "danger": True,
                        "reason": True,
                        "hidden": {"name": item.name},
                    }
                    if principal.role is Role.ADMIN
                    else "",
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
    admin = principal.role is Role.ADMIN

    def revoke(item: dict[str, Any]) -> Any:
        # The row's own action, never a typed ID (crucible#127). Revoking is hard to
        # reverse, so it asks for a reason (crucible#117).
        if not admin or item["disabled_at"] is not None:
            return ""
        return {
            "kind": "form",
            "action": "/ui/actions/token-revoke",
            "label": "Revoke",
            "danger": True,
            "reason": True,
            "hidden": {"principal_id": item["id"]},
        }

    sections: list[dict[str, Any]] = [
        {
            "title": "Principals",
            "columns": ["Name", "Role", "Created", "Revoked", ""],
            "rows": [
                [
                    item["name"],
                    item["role"],
                    item["created_at"],
                    item["disabled_at"] or "no",
                    revoke(item),
                ]
                for item in items
            ],
        }
    ]
    if admin:
        sections.append(
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
                        {"name": "reason", "label": "Reason"},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tokens",
        heading="Tokens",
        intro="Who can sign in or call the API, and with which role.",
        sections=sections,
    )


@router.get("/github", response_class=HTMLResponse)
def github_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    sections: list[dict[str, Any]] = [
        _document_section("App and repository connectivity", github.status(ctx.admin, uow))
    ]
    if principal.role is Role.ADMIN:
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
        intro="App identity, key presence, installation coverage, and last API results.",
        sections=sections,
    )


@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = status.workers(uow)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading="Active workers",
        intro="Attempts running now. Open a row's log to follow it.",
        sections=[
            {
                "title": "Active attempts",
                "empty": "No attempt is running.",
                "columns": ["Task", "Harness", "Model", "State", "Started", "Heartbeat", ""],
                "rows": [
                    [
                        item.get("external_id") or item.get("task_id"),
                        item.get("harness"),
                        item.get("model"),
                        item.get("state"),
                        item.get("started_at"),
                        item.get("last_heartbeat"),
                        # The row's own log, never a typed attempt ID (crucible#127).
                        {
                            "kind": "link",
                            "href": f"/ui/workers/{quote(str(item.get('attempt_id')))}/logs",
                            "label": "Log",
                        },
                    ]
                    for item in rows
                ],
                "details": [
                    {
                        "title": "Identifiers",
                        "columns": ["Attempt", "Task", "Image"],
                        "rows": [
                            [item.get("attempt_id"), item.get("task_id"), item.get("image_digest")]
                            for item in rows
                        ],
                    }
                ],
            }
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
    document = status.tasks(uow)
    attention = [
        [item["external_id"] or item["id"], _state_words(state), item["updated_at"]]
        for state, items in document["lists"].items()
        for item in items
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
                "title": "Tasks by state",
                "empty": "No tasks yet.",
                "columns": ["State", "Tasks"],
                "rows": [
                    [_state_words(state), count]
                    for state, count in sorted(document["counts"].items())
                ],
            },
        ],
    )


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
        return {"kind": "actions", "items": entries}

    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        heading="Bootstrap imports",
        intro="Ledgers imported from Foundry, and the commit that makes one authoritative.",
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


@router.post("/actions/{action}")
async def action(request: Request, action: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
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
        elif action == "credential-set":
            await credentials.set_api_key(
                ctx.admin,
                uow,
                principal=principal.name,
                harness="hermes",
                api_key=form.get("api_key", ""),
                reason=reason,
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
                harness=form.get("harness", ""),
                digest=form.get("digest", ""),
                reason=reason,
            )
        elif action == "harness-test":
            result = await harness_test.test_harness(
                ctx.admin, uow, principal=principal.name, harness=form.get("harness", "")
            )
            uow.commit()
            failed = result["failed_step"]
            message = (
                f"{result['harness']} passed every step."
                if result["ok"]
                else f"{result['harness']} failed at {failed}: "
                + next(s["detail"] for s in result["steps"] if s["name"] == failed)
            )
            return _redirect(form, message, kind="ok" if result["ok"] else "bad")
        elif action == "image-rollback":
            images.rollback(
                ctx.admin,
                uow,
                principal=principal.name,
                harness=form.get("harness", ""),
                reason=reason,
            )
        elif action == "routing-clear":
            routing.clear_exhaustion(
                ctx.admin, uow, principal=principal.name, pool=form.get("pool", ""), reason=reason
            )
        elif action == "routing-local":
            routing.save_local_endpoint(
                ctx.admin,
                uow,
                principal=principal,
                endpoint_url=form.get("endpoint_url", ""),
                models=[
                    {
                        "id": form.get("model_id", ""),
                        "enabled": form.get("enabled") == "true",
                        "enable_thinking": form.get("enable_thinking") == "true",
                    }
                ],
                max_concurrency=int(form.get("max_concurrency", "0")),
                reason=reason,
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
            tokens.revoke(
                ctx.admin,
                uow,
                principal=principal.name,
                principal_id=form.get("principal_id", ""),
                reason=reason,
            )
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
