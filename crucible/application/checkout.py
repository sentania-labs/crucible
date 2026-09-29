"""Private repository checkout (ADR 0019, crucible#157).

A repository registered as private is cloned with a GitHub App installation token that
is scoped to that one repository with `contents: read` and nothing else. The supervisor
mints it immediately before the preparation step, hands it to the provider's `prepare`
(which gives it to the preparer and the reference-cache refresher, never the worker),
and revokes and discards it as soon as `prepare` returns, whichever way it returned.

Registration makes the same mint once and throws the token away, so a private
repository the App cannot read is refused when it is registered rather than when its
first task starts. Public repositories never reach any of this: they clone with no
credential, as they always have.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from crucible.application.publish import repository_slug
from crucible.domain.entities import Repository
from crucible.ports.github import GitHubClient, GitHubError, InstallationToken

log = logging.getLogger("crucible.checkout")


class CheckoutRefusedError(Exception):
    """A private repository cannot be cloned, in words an operator can act on."""


def github_connected(github: GitHubClient | None) -> bool:
    """A client exists and, when it can say so, an App credential is in place."""
    if github is None:
        return False
    configured = getattr(github, "configured", None)
    return not callable(configured) or bool(configured())


def _refusal(repository: Repository, slug: str, exc: Exception) -> CheckoutRefusedError:
    installation = repository.installation_id
    if isinstance(exc, GitHubError):
        if exc.response_class == "rate_limited":
            detail = (
                f"GitHub's rate limit refused the App's request for {slug}; it asks for "
                f"{int(exc.retry_after or 60)}s before the next call"
            )
        elif exc.status == 404:
            detail = (
                f"GitHub has no installation {installation} for this App (HTTP 404); "
                "register the repository under the installation that covers it"
            )
        elif exc.status == 422:
            detail = (
                f"installation {installation} cannot read {slug}'s contents (HTTP 422); "
                "the App needs the Contents read permission and must be installed on "
                "the repository"
            )
        elif exc.status in (401, 403):
            detail = f"GitHub refused the App's request for {slug} (HTTP {exc.status})"
        elif exc.status == 0:
            detail = "the GitHub API could not be reached"
        else:
            detail = f"GitHub answered HTTP {exc.status} for {slug}"
    else:
        detail = f"the GitHub App could not mint a token ({type(exc).__name__})"
    return CheckoutRefusedError(f"{repository.name} is private: {detail}")


def mint_checkout_token(
    github: GitHubClient | None, repository: Repository
) -> InstallationToken | None:
    """The checkout token for a private repository, or None for a public one.

    Blocking. Raises `CheckoutRefusedError` when the App is not connected, the
    repository has no installation id, or the installation cannot read it."""
    if not repository.private:
        return None
    if github is None or not github_connected(github):
        raise CheckoutRefusedError(
            f"{repository.name} is private and no GitHub App is connected, so it cannot be "
            "cloned; connect the App on the GitHub page"
        )
    if repository.installation_id is None:
        raise CheckoutRefusedError(
            f"{repository.name} is private and names no GitHub App installation, so it "
            "cannot be cloned; register it again with its installation id"
        )
    slug = repository_slug(repository)
    try:
        return github.checkout_token(installation_id=repository.installation_id, repository=slug)
    except Exception as exc:
        raise _refusal(repository, slug, exc) from None


def drop_checkout_token(github: GitHubClient | None, token: InstallationToken) -> None:
    """Revoke the token at GitHub, then empty it in memory. Blocking. A revocation
    that fails is logged and not raised: the token is read-only, scoped to one
    repository, and expires within the hour whatever happens here."""
    try:
        if github is not None and not github.revoke_token(token):
            log.warning(
                "checkout token revocation was not confirmed",
                extra={"repository": token.repository},
            )
    except Exception as exc:
        log.warning(
            "checkout token revocation failed",
            extra={"repository": token.repository, "error": type(exc).__name__},
        )
    finally:
        token.discard()


def check_private_checkout(github: GitHubClient | None, repository: Repository) -> None:
    """Registration's check: mint the checkout token once and throw it away. Raises
    `CheckoutRefusedError` with the reason when a task could not clone the repository."""
    token = mint_checkout_token(github, repository)
    if token is not None:
        drop_checkout_token(github, token)


async def checkout_token_for(
    github: GitHubClient | None, repository: Repository | None
) -> InstallationToken | None:
    """`mint_checkout_token` off the event loop, for the supervisor."""
    if repository is None or not repository.private:
        return None
    return await asyncio.to_thread(mint_checkout_token, github, repository)


async def release_checkout_token(
    github: GitHubClient | None, token: InstallationToken | None
) -> None:
    """`drop_checkout_token` off the event loop. Never raises."""
    if token is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.to_thread(drop_checkout_token, github, token)
    token.discard()
