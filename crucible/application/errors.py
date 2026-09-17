"""Application errors. The API maps each to an RFC 9457 problem type."""

from __future__ import annotations

from typing import Any

from crucible.domain.entities import Event


class ApplicationError(Exception):
    slug = "application-error"
    status = 500
    title = "Application error"

    def __init__(
        self,
        detail: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        event: Event | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []
        # An event to record on its own after the request transaction has rolled back.
        self.event = event


class NotFoundError(ApplicationError):
    slug = "not-found"
    status = 404
    title = "Not found"


class ContractValidationError(ApplicationError):
    slug = "contract-invalid"
    status = 422
    title = "Task contract failed validation"


class ConflictError(ApplicationError):
    slug = "conflict"
    status = 409
    title = "Conflict"


class DuplicateExternalIdError(ConflictError):
    slug = "external-id-exists"
    title = "external_id already exists for this principal"


class TransitionNotAllowedError(ConflictError):
    slug = "transition-not-allowed"
    title = "Transition not allowed in the current state"


class ForbiddenError(ApplicationError):
    slug = "forbidden"
    status = 403
    title = "Forbidden"


class UnauthorizedError(ApplicationError):
    slug = "unauthorized"
    status = 401
    title = "Unauthorized"


class IdempotencyKeyReuseError(ApplicationError):
    slug = "idempotency-key-reuse"
    status = 422
    title = "Idempotency-Key reused with a different body"


class SupervisorNotLiveError(ApplicationError):
    """25: an administrative mutation is refused when no live supervisor holds the lease,
    so a stale instance cannot administer."""

    slug = "supervisor-not-live"
    status = 503
    title = "No live supervisor holds the lease"


class IdempotencyInProgressError(ApplicationError):
    slug = "idempotency-in-progress"
    status = 409
    title = "A request with this Idempotency-Key is still in progress"
