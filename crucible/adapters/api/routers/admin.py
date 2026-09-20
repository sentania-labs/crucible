"""/v1/admin (25), admin role only, and /v1/capabilities for the orchestrator's read-only
view. Every handler is a thin call into crucible/application/admin/*; the CLI calls the
same functions in process."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from crucible.adapters.api.deps import Admin, Ctx, Orchestrator, UoW
from crucible.application.admin import audit, credentials, github, harnesses, images, login, routing
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


@router.post("/admin/credentials/{harness}/login")
def admin_login(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """25: starts the harness's own login pointed at the dedicated directory and returns
    the device or browser URL; poll with GET, submit a pasted code with /login/code,
    and finish with /login/finish once the CLI has exited. `replace` is required when a
    credential that still passes the shape check is in place, and retains it first.

    The login runs the harness's own CLI, which lives in the worker images and not in the
    Crucible service image, so on a normal deployment this refuses with that reason and
    the operator runs `crucible-admin credentials login` where the CLI is (c5.md)."""
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
    harness: str, ctx: Ctx, _principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    return login.submit_code(ctx.logins, harness, str(body.get("code", "")))


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


@router.get("/admin/audit")
def admin_audit(
    uow: UoW,
    _principal: Admin,
    cursor: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return audit.tail(uow, cursor=cursor, limit=limit)
