"""App JWT and installation tokens (12, 23, S10).

The private key is read from the configured path into process memory for the length of
one signature and is never held on the object. Signing is RS256 with `iat` sixty seconds
in the past (GitHub rejects a clock a little fast) and a short `exp`.

Installation tokens are requested scoped to the one repository the job needs, and
optionally narrowed below the App's grant through the `permissions` field: a release
publisher that only pushes a tag needs `contents: write` and nothing else (S10, spec 24).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from crucible.ports.github import GitHubError, InstallationToken

log = logging.getLogger("crucible.github.auth")

JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60
# Re-mint before the hour is out, and well before: a publisher that outlives ten minutes
# is treated as failed (S10), so a token with two minutes left is no use to anyone.
TOKEN_REFRESH_MARGIN_SECONDS = 300


class AppKeyError(Exception):
    """The configured App private key is missing or is not an RSA private key."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    """Read the PEM into memory. Never returned to a caller that could log it."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise AppKeyError(f"the GitHub App private key at {path} could not be read: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise AppKeyError(f"the file at {path} is not a usable PEM private key") from exc
    finally:
        del data
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AppKeyError(f"the key at {path} is not an RSA private key; GitHub Apps sign RS256")
    return key


def sign_jwt(app_id: int, key: rsa.RSAPrivateKey, *, now: float | None = None) -> str:
    """An App JWT (RS256). Returned to the caller that immediately exchanges it."""
    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_LIFETIME_SECONDS,
        "iss": str(app_id),
    }
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        for part in (header, payload)
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


@dataclass(slots=True)
class _CachedToken:
    """One minted token, held in process memory until shortly before it expires."""

    value: str
    expires_at: datetime
    permissions: dict[str, str]

    def issue(self, repository: str) -> InstallationToken:
        return InstallationToken(
            self.value,
            expires_at=self.expires_at,
            repository=repository,
            permissions=dict(self.permissions),
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    app_id: int
    private_key_path: str
    api_base: str = "https://api.github.com"


class AppAuthenticator:
    """Mints repository-scoped installation tokens from the mounted key.

    Keeps a per-(installation, repository, permissions) token in memory until shortly
    before it expires. Nothing is written anywhere: the cache is a process dictionary and
    a restart simply mints again.

    Each call returns its *own* `InstallationToken`, never the cached one: a caller that
    discards its token when the job ends must not empty the cache for everyone else,
    which is exactly the bug a shared object produces."""

    def __init__(self, config: AppConfig, transport: Any) -> None:
        self._config = config
        self._transport = transport
        self._cache: dict[tuple[int, str, str], _CachedToken] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def app_jwt(self) -> str:
        key = load_private_key(self._config.private_key_path)
        try:
            return sign_jwt(self._config.app_id, key)
        finally:
            del key

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        """S10 follow-up 5: the installation id is recorded at repository registration, so
        the two discovery calls are off the hot path and this is one request."""
        short = repository.rsplit("/", maxsplit=1)[-1]
        cache_key = (installation_id, short, json.dumps(permissions or {}, sort_keys=True))
        cached = self._cache.get(cache_key)
        if cached is not None:
            remaining = (cached.expires_at - self._now()).total_seconds()
            if remaining > TOKEN_REFRESH_MARGIN_SECONDS:
                return cached.issue(repository)
            self._cache.pop(cache_key, None)
        body: dict[str, Any] = {"repositories": [short]}
        if permissions:
            body["permissions"] = dict(permissions)
        status, payload, _ = self._transport.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            body=body,
            bearer=self.app_jwt(),
        )
        if status != 201 or not isinstance(payload, dict):
            raise GitHubError(
                status,
                "could not mint an installation token",
                path=f"/app/installations/{installation_id}/access_tokens",
            )
        value = str(payload.get("token", ""))
        if not value:
            raise GitHubError(status, "the mint response carried no token")
        expires = _parse_expiry(payload.get("expires_at"))
        entry = _CachedToken(
            value=value,
            expires_at=expires,
            permissions={str(k): str(v) for k, v in (payload.get("permissions") or {}).items()},
        )
        self._cache[cache_key] = entry
        token = entry.issue(repository)
        log.info(
            "installation token minted",
            extra={
                "repository": repository,
                "installation_id": installation_id,
                "expires_at": expires.isoformat(),
                "permissions": sorted(token.permissions),
            },
        )
        return token

    def discard_all(self) -> None:
        self._cache.clear()


def _parse_expiry(value: object) -> datetime:
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC) + timedelta(minutes=55)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
