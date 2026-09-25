"""GitHub health (25): the App's public identity, whether its key is present and its
public-key fingerprint, and per registered repository whether an installation covers it
and what the last check found. `check` mints a token per repository and discards it.

Connect GitHub (crucible#120, ADR 0017): the operator enters an existing App's id and
private key, the service checks them against `GET /app` before it stores anything, and
then owns the credential (the `crucible-github-app` Secret on Kubernetes, the files
beside `github.app.private_key_path` with Docker). The App's install link comes from its
own `html_url`. The repository picker lists what each installation covers, grouped by
account, and registers a pick with the installation id and the default branch GitHub
reports. A private repository is listed but not registered: the preparation step clones
without a credential (`scripts.preparer_script`), so its first task could only fail.
Private checkout is a separate operator decision."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
)
from crucible.application.admin.repositories import register as register_repository
from crucible.application.errors import ConflictError, ContractValidationError
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.events import EventKind
from crucible.ports.github import AppCredential, GitHubAppStoreError, GitHubError
from crucible.ports.repository import UnitOfWork


class GitHubConnectError(ConflictError):
    slug = "github-connect"
    title = "GitHub App refused"


PRIVATE_NOT_SUPPORTED = "private: not supported yet"


def unsupported(repository: dict[str, Any]) -> str | None:
    """Why the picker cannot register this repository, in the words the page, the API
    and the CLI all show, or None when it can."""
    if repository.get("private"):
        return PRIVATE_NOT_SUPPORTED
    return None


def key_fingerprint(path: str | None) -> str | None:
    """sha256 of the public key's DER form, derived from the private key in memory.
    The private key never leaves the process and is never part of the answer."""
    if not path or not Path(path).is_file():
        return None
    try:
        return fingerprint_of(Path(path).read_bytes())
    except OSError:
        return None


def fingerprint_of(pem: bytes) -> str | None:
    """The public-key fingerprint of a PEM private key already in memory, or None when
    it is not one."""
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        key = serialization.load_pem_private_key(pem, password=None)
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except Exception:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()


Stored = tuple[dict[str, Any] | None, AppCredential | None]

# How long Status waits for the App credential store before it says it did not answer.
STORE_READ_TIMEOUT_SECONDS = 5.0


async def read_stored(ctx: AdminContext, *, timeout: float = STORE_READ_TIMEOUT_SECONDS) -> Stored:
    """`_stored` on a worker thread with a bounded wait, for an async handler: on
    Kubernetes it is a read of the App's Secret, and a slow API server must not hold the
    event loop."""
    store = getattr(ctx, "github_credentials", None)
    if store is None:
        return None, None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_stored, ctx), timeout)
    except TimeoutError:
        described = {
            "kind": "secret" if hasattr(store, "namespace") else "directory",
            "name": getattr(store, "name", None),
            "namespace": getattr(store, "namespace", None),
            "path": str(getattr(store, "directory", "")) or None,
            "exists": None,
            "detail": f"the App credential store did not answer within {timeout:g} seconds",
        }
        return described, None


def _stored(ctx: AdminContext) -> Stored:
    store = getattr(ctx, "github_credentials", None)
    if store is None:
        return None, None
    described = store.describe()
    try:
        credential = store.read()
    except GitHubAppStoreError:
        credential = None
    return described, credential


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


def status(ctx: AdminContext, uow: UnitOfWork, *, stored: Stored | None = None) -> dict[str, Any]:
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
    described, credential = stored if stored is not None else _stored(ctx)
    if described is None:
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
            "stored_in": None,
            "repositories": repositories,
        }
    return {
        "configured": credential is not None,
        "app_id": credential.app_id if credential else (app.app_id or None),
        "api_base": app.api_base,
        "key_present": bool(described.get("key_present")),
        "key_fingerprint": fingerprint_of(credential.private_key) if credential else None,
        "webhook_secret_present": bool(described.get("webhook_secret_present")),
        "webhook_enabled": app.webhook_enabled,
        "stored_in": {
            key: described.get(key)
            for key in ("kind", "name", "namespace", "path", "exists", "service_owned", "detail")
            if key in described
        },
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
    configured = getattr(ctx.github, "configured", None)
    if ctx.github is None or (callable(configured) and not configured()):
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
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


# ----- Connect GitHub and the repository picker (crucible#120, ADR 0017) -------------


def _install_url(app: dict[str, Any]) -> str | None:
    html_url = app.get("html_url")
    if isinstance(html_url, str) and html_url.startswith("https://"):
        return html_url.rstrip("/") + "/installations/new"
    return None


def _is_rsa(pem: bytes) -> bool:
    """GitHub App keys are RSA and JWTs are RS256; any other key is refused plainly here
    rather than failing later inside the signature."""
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

        key = serialization.load_pem_private_key(pem, password=None)
    except Exception:
        return False
    return isinstance(key, rsa.RSAPrivateKey)


def _normalized_pem(private_key: str) -> bytes:
    text = private_key.replace("\r\n", "\n").strip()
    if not text:
        raise ContractValidationError(
            "a private key is required",
            errors=[{"path": "private_key", "message": "must not be empty"}],
        )
    pem = (text + "\n").encode("utf-8")
    if not _is_rsa(pem):
        raise ContractValidationError(
            "the private key is not an RSA private key in PEM form",
            errors=[
                {
                    "path": "private_key",
                    "message": "paste the whole .pem file GitHub gave you, BEGIN and END lines "
                    "included",
                }
            ],
        )
    return pem


def _github_refusal(exc: Exception, app_id: int, api_base: str) -> GitHubConnectError:
    if isinstance(exc, GitHubError) and exc.status in (401, 403):
        return GitHubConnectError(
            f"GitHub refused the key for App {app_id} (HTTP {exc.status}); check the App ID "
            "and that the key is one of that App's private keys"
        )
    if isinstance(exc, GitHubError) and exc.status == 404:
        return GitHubConnectError(f"GitHub has no App {app_id} for this key (HTTP 404)")
    if isinstance(exc, GitHubError):
        return GitHubConnectError(
            f"GitHub answered HTTP {exc.status} when asked about App {app_id}"
        )
    return GitHubConnectError(
        f"the GitHub API at {api_base} could not be reached ({type(exc).__name__})"
    )


def connect(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    app_id: int,
    private_key: str,
    webhook_secret: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Check the id and key against `GET /app`, then store them. Nothing is stored when
    GitHub refuses them. The answer carries the App's install link, never the key."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="github connect")
    store = ctx.github_credentials
    if store is None or ctx.github_apps is None:
        raise ConflictError(
            "this deployment has nowhere to keep a GitHub App credential: run on the "
            "Kubernetes provider, or set github.app.private_key_path (ADR 0017)"
        )
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
        raise ContractValidationError(
            "the App ID must be a positive number",
            errors=[{"path": "app_id", "message": "must be a positive integer"}],
        )
    pem = _normalized_pem(private_key)
    secret = (webhook_secret or "").strip()
    try:
        app = ctx.github_apps.app(AppCredential(app_id, pem))
    except (GitHubError, OSError) as exc:
        raise _github_refusal(exc, app_id, ctx.github_app.api_base) from None
    if app.get("id") not in (app_id, str(app_id)):
        raise GitHubConnectError(
            f"GitHub says this key belongs to App {app.get('id')}, not App {app_id}"
        )
    before = status(ctx, uow)
    # The event first and the store last: the credential leaves the transaction, so a
    # refusal of the event (or anything before it) must not leave a changed credential
    # with no audit record behind it.
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_APP_CONNECTED,
        principal=principal,
        reason=reason,
        before={"app_id": before["app_id"], "key_fingerprint": before["key_fingerprint"]},
        after={
            "app_id": app_id,
            "app_slug": app.get("slug"),
            "key_fingerprint": fingerprint_of(pem),
            "webhook_secret_set": bool(secret),
            "stored_in": (before.get("stored_in") or {}).get("name")
            or (before.get("stored_in") or {}).get("path"),
        },
    )
    try:
        store.write(
            app_id=app_id,
            private_key=pem,
            webhook_secret=secret.encode("utf-8") if secret else None,
        )
    except GitHubAppStoreError as exc:
        raise ConflictError(str(exc)) from None
    return {**status(ctx, uow), "app": app, "install_url": _install_url(app)}


def apps_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """The picker: the App, its install link, and each installation's repositories,
    grouped by the account or organization it is installed on. Each repository says
    whether it is registered already, and under which name. Reads only."""
    configured = getattr(ctx.github, "configured", None)
    connected = (
        ctx.github is not None and (not callable(configured) or bool(configured()))
    ) and ctx.github_apps is not None
    view: dict[str, Any] = {
        "connected": connected,
        "app": None,
        "install_url": None,
        "error": None,
        "installations": [],
    }
    if not connected:
        view["error"] = "No GitHub App is connected. Enter its App ID and private key first."
        return view
    assert ctx.github_apps is not None
    try:
        app = ctx.github_apps.app()
        installations = ctx.github_apps.installations()
    except Exception as exc:  # the page reports a refusal, it never raises one
        view["error"] = _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base).detail
        return view
    view["app"] = app
    view["install_url"] = _install_url(app)
    registered = {
        repo.url.rstrip("/").removesuffix(".git").lower(): repo.name for repo in _registered(uow)
    }
    for installation in sorted(installations, key=lambda i: str(i.get("account") or "").lower()):
        entry: dict[str, Any] = {**installation, "error": None, "repositories": []}
        try:
            repositories = ctx.github_apps.installation_repositories(int(installation["id"]))
        except Exception as exc:  # one installation's refusal never hides the rest
            cause = getattr(exc, "status", None) or type(exc).__name__
            entry["error"] = f"its repositories could not be listed ({cause})"
            repositories = []
        for repository in sorted(repositories, key=lambda r: str(r.get("full_name")).lower()):
            url = str(repository.get("html_url") or "").rstrip("/").lower()
            entry["repositories"].append(
                {
                    **repository,
                    "registered_as": registered.get(url),
                    "unsupported": unsupported(repository),
                }
            )
        view["installations"].append(entry)
    return view


def _app_id(ctx: AdminContext) -> int:
    _, credential = _stored(ctx)
    return credential.app_id if credential else ctx.github_app.app_id


def add_repository(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    installation_id: int,
    repository: str,
    name: str | None,
    policy_name: str,
    attested_all_prs: bool,
    attested_by: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Register a repository the picker offered: the installation id, the clone URL and
    the default branch are GitHub's, read at the moment of the pick."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"github add-repository {repository}"
    )
    if ctx.github_apps is None:
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
    try:
        covered = ctx.github_apps.installation_repositories(installation_id)
    except (GitHubError, OSError) as exc:
        raise _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base) from None
    found = next(
        (r for r in covered if str(r.get("full_name")).lower() == repository.strip().lower()),
        None,
    )
    if found is None:
        raise GitHubConnectError(
            f"installation {installation_id} does not cover {repository}; install the App on "
            "it, or pick another installation"
        )
    if found.get("archived"):
        raise GitHubConnectError(f"{found['full_name']} is archived and cannot take a pull request")
    if unsupported(found):
        raise GitHubConnectError(
            f"{found['full_name']} is {PRIVATE_NOT_SUPPORTED}: the preparation step clones "
            "without a credential, so a task on it would fail before it started"
        )
    url = str(found.get("html_url") or f"https://github.com/{found['full_name']}")
    chosen = (name or "").strip() or str(found["full_name"]).rsplit("/", 1)[-1]
    existing = uow.repositories.get_by_name(chosen)
    if existing is not None and existing.url.rstrip("/").lower() != url.rstrip("/").lower():
        raise GitHubConnectError(
            f"a repository named {chosen!r} is already registered for {existing.url}; "
            "give this one another name"
        )
    return register_repository(
        ctx,
        uow,
        principal=principal,
        name=chosen,
        registration=RepositoryRegistration(
            url=url,
            default_branch=str(found.get("default_branch") or "main"),
            policy_name=policy_name,
            installation_id=installation_id,
            external_review=ExternalReviewAttestation(
                attested_all_prs=attested_all_prs, attested_by=attested_by
            ),
        ),
        reason=reason,
    )
