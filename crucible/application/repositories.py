"""Repository registration (04). Admin only. Never carries a credential."""

from __future__ import annotations

from crucible.application.errors import NotFoundError
from crucible.application.transitions import record_event
from crucible.contracts.api import RepositoryRegistration
from crucible.domain.entities import Repository
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def register_repository(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal_name: str,
    name: str,
    registration: RepositoryRegistration,
) -> Repository:
    if uow.policies.get(registration.policy_name, 1) is None:
        raise NotFoundError(f"policy {registration.policy_name!r} has no version 1")
    repository = uow.repositories.upsert(
        Repository(
            id=new_id(),
            name=name,
            url=registration.url,
            default_branch=registration.default_branch,
            policy_name=registration.policy_name,
            installation_id=registration.installation_id,
            registered_by=principal_name,
            created_at=clock.now(),
        )
    )
    record_event(
        uow,
        clock,
        EventKind.REPOSITORY_REGISTERED,
        principal=principal_name,
        payload={
            "repository": repository.name,
            "url": repository.url,
            "default_branch": repository.default_branch,
            "policy_name": repository.policy_name,
        },
    )
    uow.commit()
    return repository
