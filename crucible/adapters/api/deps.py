"""Request-scoped dependencies: settings, unit of work, authenticated principal, roles."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import Engine

from crucible.application.auth import authenticate
from crucible.application.errors import ForbiddenError, UnauthorizedError
from crucible.domain.entities import Principal, Role
from crucible.ports.clock import Clock
from crucible.ports.execution import ExecutionProvider
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory


@dataclass(slots=True)
class AppContext:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    providers: list[ExecutionProvider]
    database_url: str
    engine: Engine


def app_context(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def unit_of_work(ctx: Annotated[AppContext, Depends(app_context)]) -> Iterator[UnitOfWork]:
    with ctx.uow_factory() as uow:
        yield uow


def current_principal(
    request: Request,
    uow: Annotated[UnitOfWork, Depends(unit_of_work)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("a bearer token is required")
    principal = authenticate(uow, authorization[7:].strip())
    if principal is None:
        raise UnauthorizedError("token not recognized")
    request.state.principal = principal
    return principal


def require_mutating(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not principal.role.may_mutate:
        raise ForbiddenError(f"role {principal.role.value} may only read")
    return principal


def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role is not Role.ADMIN:
        raise ForbiddenError("admin role required")
    return principal


UoW = Annotated[UnitOfWork, Depends(unit_of_work)]
Ctx = Annotated[AppContext, Depends(app_context)]
Reader = Annotated[Principal, Depends(current_principal)]
Mutator = Annotated[Principal, Depends(require_mutating)]
Admin = Annotated[Principal, Depends(require_admin)]
