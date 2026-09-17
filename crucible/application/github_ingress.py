"""The webhook endpoint's application half (04, 23).

An optional accelerator, off by default. The adapter verifies the HMAC against the raw
body in memory and normalizes what survives; this module stores that record, the SHA-256
of the original body, and nothing else. A delivery whose signature does not match never
reaches here, so nothing of it is stored.

Processing is the supervisor's: the endpoint's job ends at the row. That keeps the API
free of GitHub I/O and makes the webhook exactly what 23 says it is, a way to make the
next poll happen sooner rather than a second source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from crucible.application.transitions import record_event
from crucible.domain.entities import GitHubDelivery
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.github.ingress")


class WebhookDisabledError(Exception):
    """The receiver is not enabled in this deployment (off by default locally)."""


@dataclass(frozen=True, slots=True)
class Accepted:
    delivery_id: str
    accepted: bool
    duplicate: bool
    detail: str


def store_delivery(
    uow: UnitOfWork,
    clock: Clock,
    *,
    delivery_id: str,
    event: str,
    action: str,
    repository: str,
    body_sha256: str,
    normalized: dict[str, Any],
    triggers_reaction_poll: bool = False,
) -> Accepted:
    """Store one verified, normalized delivery. Deduplicated by delivery id (04)."""
    stored = uow.github_deliveries.add(
        GitHubDelivery(
            delivery_id=delivery_id,
            event=event,
            action=action,
            repository=repository,
            body_sha256=body_sha256,
            normalized=dict(normalized),
            received_at=clock.now(),
        )
    )
    if not stored:
        return Accepted(delivery_id, accepted=True, duplicate=True, detail="already received")
    record_event(
        uow,
        clock,
        EventKind.GITHUB_DELIVERY_RECEIVED,
        principal=PRINCIPAL_CRUCIBLE,
        payload={
            "delivery_id": delivery_id,
            "event": event,
            "action": action,
            "repository": repository,
            "body_sha256": body_sha256,
            "triggers_reaction_poll": triggers_reaction_poll,
        },
    )
    return Accepted(delivery_id, accepted=True, duplicate=False, detail="stored")


def record_unhandled(uow: UnitOfWork, clock: Clock, *, delivery_id: str, event: str) -> Accepted:
    """An event outside the handled set: counted, nothing of it stored (23)."""
    record_event(
        uow,
        clock,
        EventKind.GITHUB_DELIVERY_RECEIVED,
        principal=PRINCIPAL_CRUCIBLE,
        payload={
            "delivery_id": delivery_id,
            "event": event,
            "handled": False,
            "note": "event not in the handled set; nothing stored (23)",
        },
    )
    return Accepted(delivery_id, accepted=False, duplicate=False, detail="event not handled")


def record_rejection(uow: UnitOfWork, clock: Clock, *, event: str, reason: str) -> None:
    """A rejected delivery is counted, and nothing of it is stored (04).

    Not even the delivery id: an unsigned request is an unauthenticated claim about
    everything in it, including which delivery it says it is."""
    record_event(
        uow,
        clock,
        EventKind.GITHUB_DELIVERY_REJECTED,
        principal=PRINCIPAL_CRUCIBLE,
        payload={
            "event": event,
            "reason": reason,
            "note": "the body was not stored and no id was recorded",
        },
    )
