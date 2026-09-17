"""Repository registration (04, 23). Admin only. Never carries a credential.

Registration is where a repository's *onboarding prerequisites* are recorded, and there
is one GitHub cannot tell Crucible: whether the external reviewer is configured to review
all pull requests there. The setting lives inside the reviewer's own product and is
absent from every GitHub surface S12 checked, so the operator attests to it and the
attestation is an event with the attesting principal and the time. A registration without
it is accepted only when the repository's policy asks for no external review round.
"""

from __future__ import annotations

from typing import Any

from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.transitions import record_event
from crucible.contracts.api import RepositoryRegistration
from crucible.domain.entities import Repository
from crucible.domain.events import EventKind
from crucible.domain.external_review import required_rounds
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
    policy = uow.policies.get(registration.policy_name, 1)
    if policy is None:
        raise NotFoundError(f"policy {registration.policy_name!r} has no version 1")
    attestation = registration.external_review
    rounds = required_rounds(policy.document)
    if rounds > 0 and not attestation.attested_all_prs:
        raise ContractValidationError(
            "this repository's policy requires external review, which needs the "
            "operator's attestation that the reviewer reviews all pull requests here",
            errors=[
                {
                    "path": "external_review.attested_all_prs",
                    "message": (
                        f"policy {policy.name}/{policy.version} sets "
                        f"external_review.required_rounds: {rounds}; GitHub exposes the "
                        "reviewer's setting nowhere, so registration records the "
                        "operator's attestation instead (23)"
                    ),
                }
            ],
        )
    now = clock.now()
    existing = uow.repositories.get_by_name(name)
    attested_by = attestation.attested_by or principal_name
    repository = uow.repositories.upsert(
        Repository(
            id=existing.id if existing else new_id(),
            name=name,
            url=registration.url,
            default_branch=registration.default_branch,
            policy_name=registration.policy_name,
            installation_id=registration.installation_id,
            registered_by=principal_name,
            created_at=existing.created_at if existing else now,
            external_review_attested=attestation.attested_all_prs,
            attested_by=attested_by if attestation.attested_all_prs else None,
            attested_at=now if attestation.attested_all_prs else None,
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
            # The installation id is a public identifier and is what keeps the two
            # discovery calls off the token-minting hot path (S10 follow-up 5).
            "installation_id": repository.installation_id,
            "external_review_attested": repository.external_review_attested,
        },
    )
    if attestation.attested_all_prs:
        payload: dict[str, Any] = {
            "repository": repository.name,
            "attested_all_prs": True,
            "attested_by": attested_by,
            "attested_at": now.isoformat(),
            "policy": {"name": policy.name, "version": policy.version},
            "required_rounds": rounds,
        }
        if attestation.note:
            payload["note"] = attestation.note
        record_event(
            uow,
            clock,
            EventKind.REPOSITORY_ATTESTATION_RECORDED,
            principal=principal_name,
            payload=payload,
        )
    return repository
