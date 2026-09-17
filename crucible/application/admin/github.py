"""GitHub health (25): the App's public identity, whether its key is present and its
public-key fingerprint, and per registered repository whether an installation covers it
and what the last check found. `check` mints a token per repository and discards it."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
)
from crucible.application.errors import ConflictError
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def key_fingerprint(path: str | None) -> str | None:
    """sha256 of the public key's DER form, derived from the private key in memory.
    The private key never leaves the process and is never part of the answer."""
    if not path or not Path(path).is_file():
        return None
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except Exception:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _last_checks(uow: UnitOfWork) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for event in uow.events.list_global(
        after_seq=0, kind=EventKind.GITHUB_CHECKED.value, since=None, limit=200
    ):
        for entry in event.payload.get("repositories", []):
            out[str(entry.get("repository"))] = {
                "checked_at": event.ts.isoformat(),
                "ok": entry.get("ok"),
                "error": entry.get("error"),
            }
    return out


def status(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    app = ctx.github_app
    last = _last_checks(uow)
    repositories: list[dict[str, Any]] = []
    for name, repo in sorted((r.name, r) for r in _registered(uow)):
        entry: dict[str, Any] = {
            "repository": name,
            "installation_covers": repo.installation_id is not None,
            "installation_id": repo.installation_id,
            "webhook_enabled": app.webhook_enabled,
        }
        entry.update({"last_check": last.get(name)})
        repositories.append(entry)
    return {
        "configured": ctx.github is not None,
        "app_id": app.app_id or None,
        "api_base": app.api_base,
        "key_present": bool(app.private_key_path and Path(app.private_key_path).is_file()),
        "key_fingerprint": key_fingerprint(app.private_key_path),
        "webhook_secret_present": bool(
            app.webhook_secret_path and Path(app.webhook_secret_path).is_file()
        ),
        "webhook_enabled": app.webhook_enabled,
        "repositories": repositories,
    }


def _registered(uow: UnitOfWork) -> list[Any]:
    return list(uow.repositories.list_all())


def check(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, reason: str | None
) -> dict[str, Any]:
    """25: mint an installation token per registered repository and discard it. The
    result per repository is a boolean and an error class; never a token."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="github check")
    if ctx.github is None:
        raise ConflictError("GitHub delivery is not configured (github.enabled)")
    results: list[dict[str, Any]] = []
    for repo in _registered(uow):
        entry: dict[str, Any] = {"repository": repo.name, "ok": False, "error": None}
        if repo.installation_id is None:
            entry["error"] = "no installation id registered"
            results.append(entry)
            continue
        try:
            token = ctx.github.installation_token(
                installation_id=repo.installation_id, repository=repo.name
            )
            entry["ok"] = True
            entry["expires_at"] = token.expires_at.isoformat()
            token.discard()
        except Exception as exc:
            entry["error"] = type(exc).__name__
        results.append(entry)
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_CHECKED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        repositories=results,
    )
    return {"repositories": results, "checked": len(results)}
