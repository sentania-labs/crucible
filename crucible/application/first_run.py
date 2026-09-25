"""The first-run administrator's token is removed after its first use (crucible#122)."""

from __future__ import annotations

import logging

from crucible.ports.first_run import FirstRunDelivery

log = logging.getLogger(__name__)

# The migration names the first-run principal with this prefix, and `token create`
# refuses it, so a name that carries it is always the migration's.
FIRST_RUN_PREFIX = "first-run-admin"


def is_first_run(name: str) -> bool:
    return name.startswith(FIRST_RUN_PREFIX)


def discard_after_use(delivery: FirstRunDelivery | None, principal_name: str) -> None:
    """Remove the delivered token once the first-run principal has used or lost it.
    Called after the sign-in or the committed revoke. Best effort: a failure is logged
    and never fails the sign-in or the revoke that caused it."""
    if delivery is None or not is_first_run(principal_name):
        return
    try:
        delivery.discard()
    except Exception as exc:  # any failure is logged, never raised
        log.warning(
            "the first-run administrator token could not be removed from %s: %s",
            delivery.where(),
            exc,
        )
