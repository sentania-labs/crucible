"""HTML administration adapter under ``/ui``."""

from __future__ import annotations

import hmac
import json
import os
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
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
    harnesses,
    images,
    login,
    repositories,
    routing,
    status,
    tokens,
)
from crucible.application.admin.context import guard_mutation
from crucible.application.auth import authenticate
from crucible.application.errors import ApplicationError, ConflictError, ForbiddenError
from crucible.application.policies import put_policy, put_routing_policy
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Principal, Role
from crucible.domain.secrets import scan_text
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


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    document = await status.status(ctx.admin, uow)
    gaps: list[list[str]] = []
    if not document["supervisor"].get("ready"):
        gaps.append(["The supervisor is not ready. Check its lease and last error.", "/ui"])
    for item in document["harnesses"]:
        if item["enabled_by_configuration"] and not item["enabled"]:
            gaps.append(
                [f"{item['name']} is disabled. Open Harnesses to enable it.", "/ui/harnesses"]
            )
        credential = item.get("credential", {})
        if credential.get("state") in ("absent", "invalid"):
            gaps.append(
                [
                    f"{item['name']} needs a credential. Open Credentials to log in.",
                    "/ui/credentials",
                ]
            )
        if item["enabled"] and not any(
            image.get("promotion_state") == "default" for image in item.get("images", [])
        ):
            gaps.append(
                [
                    f"{item['name']} has no promoted worker image. Open Images to choose one.",
                    "/ui/images",
                ]
            )
    if not uow.repositories.list_all():
        gaps.append(
            [
                "No repository is registered. Open Repositories before submitting work.",
                "/ui/repositories",
            ]
        )
    sections = []
    if gaps:
        sections.append(
            {
                "title": "Before a task can run",
                "rows": gaps,
                "columns": ["Action", "Fix page"],
            }
        )
    sections.extend(
        [
            {"title": "Supervisor", "json": document["supervisor"]},
            {"title": "Providers", "json": document["providers"]},
            {"title": "Task state", "json": document["tasks"]},
            {"title": "Pending wakes", "json": document["wakes"]},
        ]
    )
    ready = not gaps and bool(document["supervisor"].get("ready"))
    return _page(
        request,
        principal,
        csrf,
        active="/ui",
        heading="System status",
        intro="One operational view of readiness, work, providers, and actions needed.",
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
    items = harnesses.list_harnesses(ctx.admin, uow, [item for _, item in discovered])
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
    rows: list[list[Any]] = []
    for name in names:
        if name == "hermes":
            rows.append(
                [name, "not required", "Local endpoint harness", f"/ui/credentials/{name}/login"]
            )
        else:
            view = credentials.state_view(ctx.admin, uow, name)
            rows.append(
                [
                    name,
                    view.get("state"),
                    view.get("session_compatibility"),
                    f"/ui/credentials/{name}/login",
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
        options = [(name, name) for name in names if name != "hermes"]
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
            "columns": ["Harness", "Reference", "Digest", "Version", "Promotion"],
            "rows": [
                [
                    item.get("harness"),
                    item.get("reference"),
                    item.get("digest"),
                    item.get("harness_version"),
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
        intro="Images visible to providers and the explicit default per harness.",
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
    sections: list[dict[str, Any]] = [
        {"title": "Active policy", "json": policy.document if policy else {}},
        {"title": "Routing policy", "json": routing_record.document if routing_record else {}},
        {"title": "Pool exhaustion", "json": exhaustion},
    ]
    if principal.role is Role.ADMIN:
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
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    sections: list[dict[str, Any]] = [
        {"title": "App and repository connectivity", "json": github.status(ctx.admin, uow)}
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
        sections=[{"title": "Tail", "json": {"text": text[-65536:]}}],
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
        sections=[{"title": "Task state", "json": status.tasks(uow)}],
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
            {"title": "Wakes", "json": status.wakes(uow)},
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
            {"title": "Summary", "json": status.retention(uow)},
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
            {"title": "Next cursor", "json": {"next_cursor": document["next_cursor"]}},
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
        {"title": "Imports", "json": items},
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
                sections=[{"title": "Manifest", "json": document}],
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
