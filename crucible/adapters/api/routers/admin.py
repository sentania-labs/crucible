"""/v1/admin (25), admin role only, and /v1/capabilities for the orchestrator's read-only
view. Every handler is a thin call into crucible/application/admin/*; the CLI calls the
same functions in process."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from crucible.adapters.api.deps import Admin, Ctx, Orchestrator, UoW
from crucible.application.admin import (
    audit,
    credentials,
    github,
    harnesses,
    images,
    login,
    routing,
    tokens,
)
from crucible.application.admin import providers as providers_admin
from crucible.application.admin import repositories as repositories_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin.context import AdminContext
from crucible.application.errors import ConflictError
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration

router = APIRouter()


def _admin(ctx: Ctx) -> AdminContext:
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    return ctx.admin


def _reason(body: dict[str, Any] | None) -> str | None:
    """Only a non-empty string is a reason. A JSON null stringifies to "None" and a zero
    to "0", and both would pass the guard as a reason nobody wrote."""
    value = (body or {}).get("reason")
    if isinstance(value, str) and value.strip():
        return value
    return None


@router.get("/admin/routing/exhaustion")
def admin_routing_exhaustion(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return routing.list_exhaustions(_admin(ctx), uow)


@router.post("/admin/routing/exhaustion/{pool}/clear")
def admin_clear_routing_exhaustion(
    pool: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = routing.clear_exhaustion(
        _admin(ctx), uow, principal=principal.name, pool=pool, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/routing/local-endpoint")
def admin_local_endpoint(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    _admin(ctx)
    return routing.local_endpoint_view(uow)


@router.post("/admin/routing/local-endpoint")
def admin_save_local_endpoint(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    models = body.get("models")
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ConflictError("models must be a list of local model settings")
    result = routing.save_local_endpoint(
        _admin(ctx),
        uow,
        principal=principal,
        endpoint_url=str(body.get("endpoint_url", "")),
        models=models,
        max_concurrency=int(body.get("max_concurrency", 0)),
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/status")
async def admin_status(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return await status_admin.status(_admin(ctx), uow)


@router.get("/capabilities")
async def capabilities(ctx: Ctx, uow: UoW, _principal: Orchestrator) -> dict[str, Any]:
    """25: what Foundry may read: harnesses, providers, github health, workers, tasks,
    wakes. Read-only; it never calls a mutation."""
    return await status_admin.capabilities(_admin(ctx), uow)


# ----- harnesses ---------------------------------------------------------------


@router.get("/admin/harnesses")
async def admin_harnesses(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    admin = _admin(ctx)
    found = await harnesses.list_images(admin)
    return {"items": harnesses.list_harnesses(admin, uow, [i for _, i in found])}


@router.post("/admin/harnesses/{name}/enable")
def admin_enable(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = harnesses.set_enabled(
        _admin(ctx), uow, principal=principal.name, harness=name, enabled=True, reason=_reason(body)
    )
    uow.commit()
    return result


@router.post("/admin/harnesses/{name}/disable")
def admin_disable(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = harnesses.set_enabled(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=name,
        enabled=False,
        reason=_reason(body),
    )
    uow.commit()
    return result


# ----- credentials -------------------------------------------------------------


@router.get("/admin/credentials/{harness}")
def admin_credential(harness: str, ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return credentials.state_view(_admin(ctx), uow, harness)


@router.post("/admin/credentials/{harness}/validate")
async def admin_validate(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = await credentials.validate(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/probe")
async def admin_probe(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = await credentials.probe(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/set")
async def admin_set_credential(
    harness: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    value = body.get("api_key")
    if not isinstance(value, str):
        raise ConflictError("api_key must be a string")
    report = await credentials.set_api_key(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=harness,
        api_key=value,
        reason=_reason(body),
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/login")
def admin_login(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """25: starts the harness's own login pointed at the dedicated directory and returns
    the device or browser URL; poll with GET, submit a pasted code with /login/code,
    and finish with /login/finish once the CLI has exited. `replace` is required when a
    credential that still passes the shape check is in place, and retains it first.

    The login runs the harness's own CLI in the promoted worker image. The container gets
    only the selected harness credential directory and the worker egress-proxy network."""
    result = login.start_login(
        _admin(ctx),
        uow,
        ctx.logins,
        principal=principal.name,
        harness=harness,
        reason=_reason(body),
        replace=bool(body.get("replace", False)),
    )
    uow.commit()
    return result


@router.get("/admin/credentials/{harness}/login")
def admin_login_status(harness: str, ctx: Ctx, _principal: Admin) -> dict[str, Any]:
    return login.login_status(ctx.logins, harness)


@router.post("/admin/credentials/{harness}/login/code")
def admin_login_code(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.submit_code(
        ctx.logins,
        harness,
        str(body.get("code", "")),
        ctx=_admin(ctx),
        uow=uow,
        principal=principal.name,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/login/cancel")
def admin_login_cancel(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.cancel_login(
        ctx.logins,
        harness,
        ctx=_admin(ctx),
        uow=uow,
        principal=principal.name,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/login/finish")
def admin_login_finish(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.finish_login(
        _admin(ctx),
        uow,
        ctx.logins,
        principal=principal.name,
        harness=harness,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/rotate")
def admin_rotate(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = credentials.rotate(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=harness,
        new_path=str(body.get("new_path", "")),
        reason=_reason(body),
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/remove")
def admin_remove(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = credentials.remove(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


# ----- images, providers, github, repositories, audit ----------------------------


@router.get("/admin/images")
async def admin_images(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": await images.list_all(_admin(ctx), uow)}


@router.post("/admin/images/{digest:path}/promote")
async def admin_promote(
    digest: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = await images.promote(
        _admin(ctx), uow, principal=principal.name, digest=digest, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/providers")
async def admin_providers(ctx: Ctx, _principal: Admin) -> dict[str, Any]:
    return {"items": await providers_admin.providers_status(_admin(ctx))}


@router.get("/admin/github")
def admin_github(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return github.status(_admin(ctx), uow)


@router.post("/admin/github/check")
def admin_github_check(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = github.check(_admin(ctx), uow, principal=principal.name, reason=_reason(body))
    uow.commit()
    return result


@router.put("/admin/repositories/{name}")
def admin_register_repository(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """25 and 04: the same registration as PUT /repositories/{name}, under /admin so the
    table's every row has an admin path, and so under the two administrative rules: a
    reason and a live supervisor lease."""
    registration = RepositoryRegistration(
        url=str(body.get("url", "")),
        default_branch=str(body.get("default_branch", "main")),
        policy_name=str(body.get("policy_name", "default-software")),
        installation_id=body.get("installation_id"),
        external_review=ExternalReviewAttestation(
            attested_all_prs=bool(body.get("attested_all_prs", False)),
            attested_by=body.get("attested_by"),
        ),
    )
    result = repositories_admin.register(
        _admin(ctx),
        uow,
        principal=principal.name,
        name=name,
        registration=registration,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/repositories")
def admin_repositories(uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": repositories_admin.list_all(uow)}


@router.delete("/admin/repositories/{name}")
def admin_remove_repository(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = repositories_admin.remove(
        _admin(ctx), uow, principal=principal.name, name=name, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/tokens")
def admin_tokens(uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": tokens.list_principals(uow)}


@router.post("/admin/tokens")
def admin_create_token(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    minted = tokens.create(
        _admin(ctx),
        uow,
        principal=principal.name,
        name=str(body.get("name", "")),
        role=str(body.get("role", "observer")),
        reason=_reason(body),
    )
    uow.commit()
    return {
        "principal": minted.principal.name,
        "role": minted.principal.role.value,
        "token": minted.token,
    }


@router.post("/admin/tokens/{principal_id}/revoke")
def admin_revoke_token(
    principal_id: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = tokens.revoke(
        _admin(ctx),
        uow,
        principal=principal.name,
        principal_id=principal_id,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/audit")
def admin_audit(
    uow: UoW,
    _principal: Admin,
    cursor: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return audit.tail(uow, cursor=cursor, limit=limit)
