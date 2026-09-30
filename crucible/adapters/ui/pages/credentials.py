from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import CREDENTIAL_TONES, _base, _localize, _page, templates
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    credentials,
    gateway,
    login,
)
from crucible.application.errors import (
    ConflictError,
)
from crucible.domain.entities import Principal, Role

router = APIRouter(prefix="/ui", include_in_schema=False)


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
    document: dict[str, Any] = (
        {"harness": harness, "state": "not_required", "output_tail": []}
        if harness == "hermes"
        else login.login_status(ctx.logins, harness, ctx.admin)
    )
    ends = document.get("code_wait_ends_at")
    if ends:
        # hades #173: a CLI that gives up on its own (AGY, 60 seconds) says when, in the
        # operator's zone.
        settings = getattr(ctx, "settings", None)
        zone = settings.service.render_timezone if settings is not None else "America/Chicago"
        moment = datetime.fromisoformat(str(ends))
        document["code_wait_local"] = _localize(moment, zone)
        document["code_wait_seconds_left"] = max(
            0, round((moment - datetime.now(UTC)).total_seconds())
        )
    context = _base(request, principal, csrf, title=f"{harness} login", active="/ui/credentials")
    context.update(harness=harness, login=document)
    return templates.TemplateResponse(request=request, name="login.html", context=context)


async def _action_credential(
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
    return None


register("credential", _action_credential)


async def _action_login_start(
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
    login.start_login(
        ctx.admin,
        uow,
        ctx.logins,
        principal=principal.name,
        harness=form.get("harness", ""),
        reason=reason,
        replace=form.get("replace") == "true",
    )
    return None


register("login-start", _action_login_start)


async def _action_login_code(
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
    login.submit_code(
        ctx.logins,
        form.get("harness", ""),
        form.get("code", ""),
        ctx=ctx.admin,
        uow=uow,
        principal=principal.name,
        reason=reason,
    )
    return None


register("login-code", _action_login_code)


async def _action_login_cancel(
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
    login.cancel_login(
        ctx.logins,
        form.get("harness", ""),
        ctx=ctx.admin,
        uow=uow,
        principal=principal.name,
        reason=reason,
    )
    return None


register("login-cancel", _action_login_cancel)


async def _action_login_finish(
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
    login.finish_login(
        ctx.admin,
        uow,
        ctx.logins,
        principal=principal.name,
        harness=form.get("harness", ""),
        reason=reason,
    )
    return None


register("login-finish", _action_login_finish)
