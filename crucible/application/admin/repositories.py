"""Repository registration as an administrative mutation (25, 04).

04's `PUT /repositories/{name}` is the ordinary registration path. The row of 25's
operations table is the same registration under the administrative surface, and so it
obeys the two rules every administrative mutation obeys: a reason, and a live supervisor
lease. The event carries both, with the before-and-after summary, which is why this
wrapper exists rather than the router calling the legacy service directly.
"""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.repositories import register_repository
from crucible.contracts.api import RepositoryRegistration
from crucible.ports.repository import UnitOfWork


def _view(uow: UnitOfWork, name: str) -> dict[str, Any] | None:
    existing = uow.repositories.get_by_name(name)
    if existing is None:
        return None
    return {
        "repository": existing.name,
        "url": existing.url,
        "default_branch": existing.default_branch,
        "policy_name": existing.policy_name,
        "installation_id": existing.installation_id,
        "external_review_attested": existing.external_review_attested,
    }


def register(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    registration: RepositoryRegistration,
    reason: str | None,
) -> dict[str, Any]:
    """The admin path's registration: guarded, and the event says who, why, and what it
    replaced. The returned document is the same on both entry points."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"repositories register {name}"
    )
    before = _view(uow, name)
    repo = register_repository(
        uow,
        ctx.clock,
        principal_name=principal,
        name=name,
        registration=registration,
        reason=reason,
        before=before,
    )
    return {
        "repository": repo.name,
        "id": repo.id,
        "url": repo.url,
        "default_branch": repo.default_branch,
        "policy_name": repo.policy_name,
        "installation_id": repo.installation_id,
        "external_review_attested": repo.external_review_attested,
    }
