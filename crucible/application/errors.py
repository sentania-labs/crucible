"""Application errors. The API maps each to an RFC 9457 problem type."""

from __future__ import annotations

from typing import Any


class ApplicationError(Exception):
    slug = "application-error"
    status = 500
    title = "Application error"

    def __init__(self, detail: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


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
