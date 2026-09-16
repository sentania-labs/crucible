"""Idempotency-Key on creating POSTs (04): same key and body replays the stored
response; same key with a different body is 422 idempotency-key-reuse."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from crucible.application.errors import IdempotencyKeyReuseError
from crucible.domain.entities import Principal
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWorkFactory

Producer = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


def body_sha256(body: bytes, scope: str = "") -> str:
    """Hash of the canonical body, scoped by route so one key cannot straddle two endpoints."""
    try:
        canonical = json.dumps(json.loads(body or b"{}"), sort_keys=True, separators=(",", ":"))
    except ValueError:
        canonical = body.decode("utf-8", "replace")
    return hashlib.sha256(f"{scope}\n{canonical}".encode()).hexdigest()


async def with_idempotency(
    *,
    uow_factory: UnitOfWorkFactory,
    clock: Clock,
    principal: Principal,
    key: str | None,
    body: bytes,
    scope: str,
    produce: Producer,
) -> JSONResponse:
    if key is None:
        status, payload = await produce()
        return JSONResponse(status_code=status, content=payload)
    digest = body_sha256(body, scope)
    with uow_factory() as uow:
        stored = uow.idempotency.get(principal.id, key)
    if stored is not None:
        stored_digest, status, payload = stored
        if stored_digest != digest:
            raise IdempotencyKeyReuseError(
                f"Idempotency-Key {key!r} was used with a different request body"
            )
        return JSONResponse(
            status_code=status, content=payload, headers={"Idempotent-Replayed": "true"}
        )
    status, payload = await produce()
    try:
        with uow_factory() as uow:
            uow.idempotency.put(
                principal.id,
                key,
                request_sha256=digest,
                status=status,
                body=payload,
                now=clock.now(),
            )
            uow.commit()
    except IntegrityError:
        # A concurrent first request stored the key; the mutation above still succeeded,
        # so the caller gets its own result rather than a 500.
        pass
    return JSONResponse(status_code=status, content=payload)
