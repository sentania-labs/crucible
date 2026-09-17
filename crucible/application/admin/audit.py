"""The audit tail (25): admin events only, with a cursor."""

from __future__ import annotations

from typing import Any

from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork

ADMIN_KINDS: frozenset[str] = frozenset(
    {
        EventKind.HARNESS_ENABLED.value,
        EventKind.HARNESS_DISABLED.value,
        EventKind.CREDENTIAL_VALIDATED.value,
        EventKind.CREDENTIAL_PROBED.value,
        EventKind.CREDENTIAL_LOGIN_STARTED.value,
        EventKind.CREDENTIAL_LOGIN_FINISHED.value,
        EventKind.CREDENTIAL_ROTATED.value,
        EventKind.CREDENTIAL_REMOVED.value,
        EventKind.CREDENTIAL_RETIRED_SHREDDED.value,
        EventKind.IMAGE_PROMOTED.value,
        EventKind.GITHUB_CHECKED.value,
        EventKind.ADMIN_REFUSED.value,
        EventKind.PRINCIPAL_CREATED.value,
        EventKind.REPOSITORY_REGISTERED.value,
        EventKind.REPOSITORY_ATTESTATION_RECORDED.value,
        EventKind.POLICY_UPLOADED.value,
        EventKind.ROUTING_POLICY_UPLOADED.value,
    }
)


def tail(uow: UnitOfWork, *, cursor: int | None, limit: int) -> dict[str, Any]:
    """Admin events after the cursor, oldest first; `next_cursor` is the last seq."""
    items: list[dict[str, Any]] = []
    after = cursor or 0
    pages = 0
    while len(items) < limit and pages < 20:
        batch = uow.events.list_global(after_seq=after, kind=None, since=None, limit=200)
        if not batch:
            break
        pages += 1
        for event in batch:
            after = event.seq or after
            if event.kind in ADMIN_KINDS:
                items.append(
                    {
                        "seq": event.seq,
                        "ts": event.ts.isoformat(),
                        "kind": event.kind,
                        "principal": event.principal,
                        "payload": event.payload,
                    }
                )
                if len(items) >= limit:
                    break
    next_cursor = items[-1]["seq"] if items else (cursor or 0)
    return {"items": items, "next_cursor": next_cursor}
