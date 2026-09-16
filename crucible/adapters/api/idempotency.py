"""Idempotency-Key on creating POSTs (04).

The key row is reserved inside the mutation's transaction and completed with the
response in that same transaction, so either both the mutation and the record commit
or neither does. A repeat with the same key and body replays the stored response; the
same key with a different body is 422 idempotency-key-reuse. Two concurrent first
requests serialize on the unique index: the second waits for the first to commit, then
replays it. The mutation never runs twice."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse

from crucible.application.errors import IdempotencyInProgressError, IdempotencyKeyReuseError
from crucible.domain.entities import Principal
from crucible.ports.clock import Clock
from crucible.ports.repository import IdempotencyKeyTakenError, UnitOfWork, UnitOfWorkFactory

Producer = Callable[[UnitOfWork], Awaitable[tuple[int, dict[str, Any]]]]
REPLAYED_HEADER = "Idempotent-Replayed"


def body_sha256(body: bytes, scope: str = "") -> str:
    """Hash of the canonical body, scoped by route so one key cannot straddle two endpoints."""
    try:
        canonical = json.dumps(json.loads(body or b"{}"), sort_keys=True, separators=(",", ":"))
    except ValueError:
        canonical = body.decode("utf-8", "replace")
    return hashlib.sha256(f"{scope}\n{canonical}".encode()).hexdigest()


def _replay(
    uow_factory: UnitOfWorkFactory, principal: Principal, key: str, digest: str
) -> JSONResponse:
    with uow_factory() as uow:
        stored = uow.idempotency.get(principal.id, key)
    if stored is None:
        raise IdempotencyInProgressError(f"Idempotency-Key {key!r} is being processed")
    stored_digest, status, payload = stored
    if stored_digest != digest:
        raise IdempotencyKeyReuseError(
            f"Idempotency-Key {key!r} was used with a different request body"
        )
    if status is None or payload is None:
        raise IdempotencyInProgressError(f"Idempotency-Key {key!r} is being processed")
    return JSONResponse(status_code=status, content=payload, headers={REPLAYED_HEADER: "true"})


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
    """Run the mutation in one transaction, with the key reserved and completed inside it."""
    if key is None:
        with uow_factory() as uow:
            status, payload = await produce(uow)
            uow.commit()
        return JSONResponse(status_code=status, content=payload)
    digest = body_sha256(body, scope)
    try:
        with uow_factory() as uow:
            uow.idempotency.reserve(principal.id, key, request_sha256=digest, now=clock.now())
            status, payload = await produce(uow)
            uow.idempotency.complete(principal.id, key, status=status, body=payload)
            uow.commit()
    except IdempotencyKeyTakenError:
        return _replay(uow_factory, principal, key, digest)
    return JSONResponse(status_code=status, content=payload)
