"""Bearer tokens (04): `cru_<principal ulid>.<secret>`; stored as a salted SHA-256.

The secret is 32 random bytes, so the salt guards against precomputation and the
entropy against brute force. Verification is constant-time. Values never leave
this module except the one-time return from mint_token."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from crucible.domain.entities import Principal, Role
from crucible.domain.ids import is_ulid, new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

TOKEN_PREFIX = "cru_"
SALT_BYTES = 16
SECRET_BYTES = 32


def _digest(salt: bytes, secret: str) -> bytes:
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class MintedToken:
    principal: Principal
    token: str


def mint_token(
    uow: UnitOfWork, clock: Clock, *, name: str, role: Role, rotate: bool = False
) -> MintedToken:
    """Create a principal with a fresh token, or rotate an existing principal's token."""
    existing = uow.principals.get_by_name(name)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    if existing is not None:
        if not rotate:
            raise ValueError(f"principal {name!r} exists; pass rotate to replace its token")
        uow.principals.rotate(existing.id, salt, _digest(salt, secret))
        return MintedToken(existing, f"{TOKEN_PREFIX}{existing.id}.{secret}")
    principal = Principal(id=new_id(), name=name, role=role, created_at=clock.now())
    uow.principals.add(principal, salt, _digest(salt, secret))
    return MintedToken(principal, f"{TOKEN_PREFIX}{principal.id}.{secret}")


def authenticate(uow: UnitOfWork, token: str) -> Principal | None:
    """Resolve a bearer token to its principal, or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    principal_id, sep, secret = token[len(TOKEN_PREFIX) :].partition(".")
    if not sep or not is_ulid(principal_id) or not secret:
        return None
    credentials = uow.principals.credentials(principal_id)
    if credentials is None:
        return None
    salt, stored = credentials
    if not hmac.compare_digest(stored, _digest(salt, secret)):
        return None
    return uow.principals.get(principal_id)
