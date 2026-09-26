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
from crucible.application.admin import limits as limits_admin
from crucible.application.admin.context import guard_mutation
from crucible.application.admin.providers import providers_status
from crucible.application.auth import authenticate
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
    ContractValidationError,
    ForbiddenError,
)
from crucible.application.first_run import discard_after_use
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
    ("/ui/credentials", "Credentials"),
    ("/ui/gateway", "Local gateway"),
    ("/ui/images", "Images"),
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
# The name is compared whole, or by a credential prefix or suffix, and the names that
# only look like one are listed as what they are: a harness called `claude_code` is a
# harness, not a login code, and its version is not a secret.
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
SECRET_SUFFIXES = ("_api_key", "_code", "_password", "_private_key", "_secret", "_token")
SECRET_PREFIXES = (
    "api_key_",
    "authorization_",
    "password_",
    "private_key_",
    "secret_",
    "token_",
)
# Names that describe a credential without holding one: whether it is there, what it
# fingerprints to, where it is kept, when it changed.
DESCRIBES_SECRET_SUFFIXES = ("_at", "_fingerprint", "_path", "_present", "_set", "_source")
NON_SECRET_FIELDS = {
    "error_code",
    "exit_code",
    "fenced_token",
    "http_code",
    "status_code",
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
NO_REASON_ACTIONS = frozenset(
    {"/ui/actions/github-check", "/ui/actions/harness-test", "/ui/actions/gateway-test"}
)
# A row action names its reason mode itself, since one action path can serve both a
# check and a removal (credential validate and remove): `True` is required (destructive),
# "optional" is an audit note the operator may leave out, absent asks for none.


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
    return (
        name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.startswith(SECRET_PREFIXES)
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
        if not any(isinstance(item, (dict, list)) for item in value):
            # A plain list reads as a list, not a one-column table headed "Value"
            # (crucible#115).
            return {"kind": "values", "items": [_safe_value(key, item) for item in value]}
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


def _without_migration(note: Any) -> str:
    """A model note without the migration that wrote it (crucible#115)."""
    return re.sub(r" \(\d{4}_[a-z0-9_]+\)", "", str(note))


def _check_words(check: Any) -> str:
    """A repository's last connectivity check in one phrase."""
    if not isinstance(check, dict) or not check:
        return "not checked yet"
    when = check.get("checked_at") or check.get("at") or ""
    outcome = "passed" if check.get("ok") else f"failed: {check.get('error') or 'no detail'}"
    return f"last check {outcome} {when}".strip()


def _duration_words(milliseconds: Any) -> str:
    """A millisecond bound in the unit an operator reads it in."""
    try:
        value = int(milliseconds)
    except (TypeError, ValueError):
        return str(milliseconds)
    for unit, size in (("hour", 3_600_000), ("minute", 60_000), ("second", 1000)):
        if value >= size and value % size == 0:
            count = value // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{value} ms"


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
    intro = str(_localize(intro, timezone))
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
CREDENTIAL_TONES = {
    "validated": "ok",
    "valid": "ok",
    "not_required": "accent",
    "configured": "warn",
    "absent": "warn",
    "invalid": "bad",
    "unreadable": "bad",
}


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
    readiness = document["readiness"]
    sections, (harness_summary, harness_detail) = _readiness_sections(readiness)
    supervisor = document["supervisor"]
    tasks_part = document["tasks"]
    attention = sum(len(rows) for rows in tasks_part["lists"].values())
    running = len(document["workers"])
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


# The first readiness step of a harness in one word (crucible#115, #123).
STEP_WORDS = {
    "disabled": "disabled",
    "credential_missing": "needs a credential",
    "credential_unreadable": "credential unreadable",
    "credential_invalid": "credential refused",
    "credential_not_verified": "credential not verified",
    "endpoint_not_configured": "needs the gateway",
    "no_enabled_model": "needs a model",
    "endpoint_unreachable": "gateway unreachable",
    "no_promoted_image": "needs an image",
    "promoted_image_missing": "image no longer listed",
}


def _harness_status(item: dict[str, Any], ready: dict[str, Any] | None) -> dict[str, Any]:
    """The one word an operator acts on, most blocking first, from the same readiness
    Status shows. A test fixture has no readiness entry and is judged on its image."""
    if not item["enabled_by_configuration"]:
        return {"kind": "status", "value": "off in configuration", "tone": "bad"}
    if ready is not None and ready["steps"]:
        step = ready["steps"][0]
        return {
            "kind": "status",
            "value": STEP_WORDS.get(step["code"], "not ready"),
            "tone": "warn",
            "hint": step["text"] if len(ready["steps"]) == 1 else None,
        }
    if ready is None and not item["enabled"]:
        return {"kind": "status", "value": "disabled", "tone": "warn"}
    if ready is None and not item.get("default_image"):
        return {"kind": "status", "value": "needs an image", "tone": "warn"}
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
    items, secrets = await harnesses.read_harnesses(
        ctx.admin, uow, [item for _, item in discovered]
    )
    ready_by_name = {
        entry["name"]: entry
        for entry in status.harness_readiness(
            ctx.admin, uow, items, await providers_status(ctx.admin), secrets
        )
    }
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
                    "reason": "optional",
                    "hidden": {
                        "harness": name,
                        "enabled": "false" if item["enabled_by_administrator"] else "true",
                    },
                }
            )
        rows.append(
            [
                name,
                _harness_status(item, ready_by_name.get(name)),
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
    secrets = await credentials.read_secrets(ctx.admin, names)
    # Where the credentials are Secrets the service owns (Kubernetes, ADR 0015), rotate
    # and remove move and shred directories and are refused, so they are not offered
    # (crucible#125).
    secrets_held = credentials.secret_store(ctx.admin) is not None
    admin = principal.role is Role.ADMIN
    rows: list[list[Any]] = []
    compatibility: list[list[Any]] = []
    for name in names:
        view = credentials.state_view(ctx.admin, uow, name, secrets.get(name))
        state = str(view.get("state") or "")
        needed = state != "not_required"
        actions: list[dict[str, Any]] = []
        # Hermes has no login: its key and gateway URL are set together (#119). A harness
        # that needs no credential has neither (crucible#125).
        if name == credentials.HERMES:
            actions.append({"kind": "link", "href": "/ui/gateway", "label": "Local gateway"})
        elif needed:
            actions.append(
                {"kind": "link", "href": f"/ui/credentials/{name}/login", "label": "Log in"}
            )
        # Validate, probe and remove act on a stored credential; with none there is only
        # the way to set one up (crucible#115).
        if admin and needed and state != "absent":
            for verb, label in (("validate", "Validate"), ("probe", "Probe")):
                actions.append(
                    {
                        "kind": "form",
                        "action": "/ui/actions/credential",
                        "label": label,
                        "hidden": {"harness": name, "verb": verb},
                    }
                )
            if not secrets_held:
                actions.append(
                    {
                        "kind": "form",
                        "action": "/ui/actions/credential",
                        "label": "Remove",
                        "danger": True,
                        "reason": True,
                        "hidden": {"harness": name, "verb": "remove"},
                    }
                )
        rows.append(
            [
                name,
                {
                    "kind": "status",
                    "value": state.replace("_", " "),
                    "tone": CREDENTIAL_TONES.get(state, "warn"),
                },
                gateway.plain_outcome(view.get("last_launch_outcome")),
                {"kind": "actions", "items": actions} if actions else "",
            ]
        )
        compatibility.append([name, view.get("session_compatibility")])
    sections: list[dict[str, Any]] = [
        {
            "title": "Credentials",
            "columns": ["Harness", "State", "Last test", ""],
            "rows": rows,
            "details": [
                {
                    "title": "Session compatibility",
                    "columns": ["Harness", "Compatibility"],
                    "rows": compatibility,
                }
            ],
        }
    ]
    if admin and not secrets_held:
        options = [
            (name, name)
            for name in names
            if credentials.state_view(ctx.admin, uow, name, secrets.get(name)).get("state")
            != "not_required"
        ]
        sections.append(
            {
                "title": "Rotate from a prepared directory",
                "form": {
                    "action": "/ui/actions/credential",
                    "label": "Rotate",
                    "collapsed": "Rotate a credential",
                    "fields": [
                        {"name": "verb", "kind": "hidden", "value": "rotate"},
                        {
                            "name": "harness",
                            "label": "Harness",
                            "kind": "select",
                            "options": options,
                        },
                        {"name": "new_path", "label": "Prepared directory", "required": True},
                        {"name": "reason", "label": "Reason"},
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
        intro="Each harness's credential and what to do about it. Values are never shown.",
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
    """One row per harness (ADR 0018): its default, the image a rollback returns to, and
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
                    "reason": "optional",
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
                    "reason": "optional",
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
    passed = view["last_outcome"] == "probe:completed"
    # crucible#115: one row in plain words; the credential's state is on Credentials.
    sections: list[dict[str, Any]] = [
        {
            "title": "Gateway",
            "columns": ["URL", "Key", "Last test"],
            "rows": [
                [
                    view["endpoint_url"] or "not set",
                    "set" if view["key_set"] else "not set",
                    {
                        "kind": "status",
                        "value": view["last_test"],
                        "tone": "ok" if passed else "warn",
                        "hint": view["last_tested_at"] or "",
                    },
                ]
            ],
        }
    ]
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
                    _without_migration(row["note"]),
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
                    "The URL ends in /v1. The key is the LiteLLM virtual key Hermes sends; "
                    "it is stored as the Hermes credential and never shown. Saving tests "
                    "both."
                ),
                "form": {
                    "action": "/ui/actions/gateway-save",
                    "label": "Save and test",
                    "collapsed": "Change the URL or key" if view["endpoint_url"] else None,
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
            # A check: no reason is asked for (crucible#117).
            sections[0]["form"] = {
                "action": "/ui/actions/gateway-test",
                "label": "Test the gateway again",
                "fields": [{"name": "reason", "label": "Reason"}],
            }
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
                        # The note without the migration that wrote it (crucible#115).
                        {"value": _without_migration(row["note"])},
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
        intro="Which worker image each harness runs. CI proof tags (ci-*) are not listed.",
        sections=sections,
    )


@router.get("/routing", response_class=HTMLResponse)
def routing_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    versions = list(uow.policies.list_versions("default-software"))
    # The version in force: the newest one not retired, as the timeout editor reads it.
    live = [item for item in versions if item.retired_at is None]
    policy = max(live, key=lambda item: item.version) if live else None
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
    command_timeout = limits_admin.command_timeout_view(uow)
    admin = principal.role is Role.ADMIN
    bounds = command_timeout["command_timeout_ms"]
    dns = egress["document"].get("dns") or {}
    endpoint = egress["document"].get("local_endpoint") or {}
    local_models = ", ".join(m["id"] for m in local["models"] if m.get("enabled")) or "none"
    # crucible#115: what is in force, one line each, in plain words; the documents behind
    # them are under Details, and each is edited from its own form below.
    in_force: list[list[Any]] = [
        [
            "Delivery policy",
            f"{policy.name} version {policy.version}" if policy else "none",
            "",
        ],
        [
            "Routing policy",
            f"{routing_ref.get('name')} version {routing_ref.get('version')}"
            if routing_ref
            else "none",
            "",
        ],
        [
            "Local gateway",
            {
                "kind": "note",
                "value": gateway_endpoint or "not set",
                "hint": f"models in use: {local_models}",
            },
            {"kind": "link", "href": "/ui/gateway", "label": "Set up on Local gateway"},
        ],
        [
            "Per-command timeout",
            {
                "kind": "note",
                "value": f"{_duration_words(bounds['default'])} by default",
                "hint": (
                    f"a task may set {_duration_words(bounds['min'])} "
                    f"to {_duration_words(bounds['max'])}"
                ),
            },
            "",
        ],
        [
            "Kubernetes worker egress",
            {
                "kind": "note",
                "value": (
                    f"DNS: {dns.get('namespace') or 'any namespace'}; local endpoint: "
                    f"{endpoint.get('namespace') or 'outside the cluster'}"
                )
                if egress["provider_enabled"]
                else "not in use: the Kubernetes provider is off",
            },
            "",
        ],
    ]
    marks = [item for item in exhaustion["items"] if item["active"]]
    sections: list[dict[str, Any]] = [
        {
            "title": "In force",
            "columns": ["Setting", "Value", ""],
            "rows": in_force,
            "details": [
                _document_section("Local endpoint", local),
                _document_section("Kubernetes egress selectors", egress),
                _document_section("Per-command timeout", command_timeout),
                _document_section("Delivery policy document", policy.document if policy else {}),
                _document_section(
                    "Routing policy document", routing_record.document if routing_record else {}
                ),
            ],
        },
        {
            "title": "Exhausted pools",
            "empty": "No pool is marked exhausted.",
            "columns": ["Pool", "Since", "Resets", "Why", ""],
            "rows": [
                [
                    item["pool"],
                    item["exhausted_at"],
                    item["reset_at"],
                    item["reason"],
                    # The row's own action, never a typed pool name (crucible#127).
                    {
                        "kind": "form",
                        "action": "/ui/actions/routing-clear",
                        "label": "Clear",
                        "reason": "optional",
                        "hidden": {"pool": item["pool"]},
                    }
                    if admin
                    else "",
                ]
                for item in marks
            ],
            "details": [_document_section("Every mark, cleared ones too", exhaustion)]
            if exhaustion["items"]
            else [],
        },
    ]
    if admin:
        sections.append(
            {
                "title": "Edit per-command timeout",
                "note": (
                    "The timeout, in milliseconds, every harness runs a shell command under. "
                    "A task may narrow the default within min and max. Saving writes a new "
                    "delivery policy version."
                ),
                "form": {
                    "action": "/ui/actions/command-timeout",
                    "label": "Save command timeout",
                    "collapsed": "Change the per-command timeout",
                    "fields": [
                        {
                            "name": "min",
                            "label": "Minimum (ms)",
                            "kind": "number",
                            "value": bounds["min"],
                            "required": True,
                        },
                        {
                            "name": "default",
                            "label": "Default (ms)",
                            "kind": "number",
                            "value": bounds["default"],
                            "required": True,
                        },
                        {
                            "name": "max",
                            "label": "Maximum (ms)",
                            "kind": "number",
                            "value": bounds["max"],
                            "required": True,
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
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
                    "collapsed": "Change the egress selectors",
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
                        "collapsed": "Upload a routing policy document",
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
                        "collapsed": "Upload a delivery policy document",
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
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/routing",
        heading="Routing",
        intro="What routes and limits a task: the policies in force, the gateway, and pools.",
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
    """crucible#120: connect an existing App by its id and key, install it from its own
    link, then pick repositories from what each installation covers."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    state = github.status(ctx.admin, uow)
    picker = github.apps_view(ctx.admin, uow) if state["configured"] else None
    admin = principal.role is Role.ADMIN
    # crucible#115: the connection in plain words and the registered repositories first;
    # the stored-credential document is behind Details.
    connection: dict[str, Any] = {
        "title": "Connection",
        "columns": ["Part", "State"],
        "rows": [
            [
                "App",
                {
                    "kind": "status",
                    "value": f"connected, App {state['app_id']}"
                    if state["configured"]
                    else "not connected",
                    "tone": "ok" if state["configured"] else "warn",
                },
            ],
            ["Private key", state["key_fingerprint"] or "none stored"],
            ["Webhook", "on" if state["webhook_enabled"] else "off"],
            *[
                [
                    f"Repository {repo['repository']}",
                    {
                        "kind": "note",
                        "value": "covered by the installation"
                        if repo["installation_covers"]
                        else "no installation covers it",
                        "hint": _check_words(repo.get("last_check")),
                    },
                ]
                for repo in state["repositories"]
            ],
        ],
        "details": [_document_section("Stored App and every repository", state)],
    }
    if state["configured"]:
        connection["rows"].append(
            [
                "A repository the picker cannot show",
                {
                    "kind": "link",
                    "href": "/ui/repositories",
                    "label": "Register it on Repositories",
                },
            ]
        )
    if admin and state["configured"]:
        # A read-only check: no reason is asked for (crucible#117).
        connection["form"] = {
            "action": "/ui/actions/github-check",
            "label": "Check every repository",
            "fields": [{"name": "reason", "label": "Reason"}],
        }
    sections: list[dict[str, Any]] = [connection]
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
                    "collapsed": "Connect a different App" if state["configured"] else None,
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


# 26 and issue 61: the one restart-bound setting that widens what a worker can reach. The
# page intro already says every setting here is read at start (crucible#115).
_BROAD_EGRESS_REASON = (
    "On, a worker's egress is the public internet on 443 minus the denied ranges, GitHub "
    "included, instead of the resolved allowlist."
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
            else _BROAD_EGRESS_REASON
            if path == "kubernetes.broad_egress"
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


def _milliseconds(form: dict[str, str], name: str) -> int | None:
    raw = form.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ContractValidationError(
            f"{name} must be a whole number of milliseconds",
            errors=[{"path": name, "message": "a whole number of milliseconds"}],
        )
    return int(raw)


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
            await images.rollback(
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
        elif action == "command-timeout":
            limits_admin.save_command_timeout(
                ctx.admin,
                uow,
                principal=principal,
                minimum=_milliseconds(form, "min"),
                maximum=_milliseconds(form, "max"),
                default=_milliseconds(form, "default"),
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
