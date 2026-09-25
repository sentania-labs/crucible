"""The GitHub port (23, ADR 0007).

Crucible performs every routine GitHub mutation through this one interface: mint a
repository-scoped installation token, read everything observation needs, create the pull
request and update it, and delete a ref at cleanup. There is deliberately no merge call
and no issue-comment write on the default path: merging is the operator's act, and the
trigger comment is gated off (23).

Nothing here returns a token. `installation_token` hands back an opaque object whose
value is readable exactly once by the publisher's hand-over, and whose `__repr__` and
`__str__` never show it, so a token cannot reach a log through an f-string.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class GitHubError(Exception):
    """A GitHub API call failed. Carries the status and a *class* of response, never a
    body that might echo a request header."""

    def __init__(
        self, status: int, message: str, *, path: str = "", response_class: str = ""
    ) -> None:
        super().__init__(f"{status} on {path}: {message}" if path else f"{status}: {message}")
        self.status = status
        self.message = message
        self.path = path
        self.response_class = response_class or classify(status)


def classify(status: int) -> str:
    """The response class recorded on a failure event (23 step 7). No body, no headers."""
    if status == 0:
        return "transport"
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 422:
        return "unprocessable"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return "ok"


class UnobservableError(Exception):
    """A read the App's permission set does not reach.

    23: the App lacks Issues read until the operator adds it, and the PR-level reactions
    endpoint is the one place a clean external review appears. A 403 there is recorded as
    "reactions unobservable" and is not fatal."""

    def __init__(self, what: str, *, status: int = 403) -> None:
        super().__init__(f"{what} is not observable with the App's permissions ({status})")
        self.what = what
        self.status = status


class InstallationToken:
    """A short-lived installation token. In memory only, never stored, never printed.

    The value is behind a method rather than an attribute so that every read is a
    deliberate call and an accidental `f"{token}"` cannot leak it."""

    __slots__ = ("_value", "expires_at", "permissions", "repository")

    def __init__(
        self,
        value: str,
        *,
        expires_at: datetime,
        repository: str,
        permissions: dict[str, str] | None = None,
    ) -> None:
        self._value = value
        self.expires_at = expires_at
        self.repository = repository
        self.permissions = permissions or {}

    def reveal(self) -> str:
        return self._value

    def discard(self) -> None:
        self._value = ""

    def __repr__(self) -> str:
        return f"<InstallationToken repository={self.repository} expires_at={self.expires_at}>"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
    base_ref: str
    state: str
    merged: bool = False
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    merged_by: str | None = None
    closed_at: datetime | None = None
    # `GET /pulls/{n}` carries no closer; the client fills this from the issue events
    # timeline when a pull request is observed closed and unmerged (23).
    closed_by: str | None = None
    mergeable_state: str = ""
    title: str = ""
    draft: bool = False


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    github_id: str
    login: str
    state: str
    body: str
    commit_id: str | None
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class CommentRecord:
    github_id: str
    login: str
    body: str
    created_at: datetime
    updated_at: datetime
    kind: str = "review_comment"
    path: str | None = None
    line: int | None = None
    commit_id: str | None = None
    review_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReactionRecord:
    github_id: str
    login: str
    content: str
    created_at: datetime
    subject_kind: str
    subject_github_id: str


@dataclass(frozen=True, slots=True)
class CheckRecord:
    name: str
    status: str
    conclusion: str | None
    head_sha: str
    url: str = ""
    external_id: str = ""
    workflow: str = ""
    job: str = ""
    source: str = "check_run"


@dataclass(frozen=True, slots=True)
class Observation:
    """One poll of a pull request: everything 23 asks the supervisor to fetch."""

    pull_request: PullRequestRef
    reviews: tuple[ReviewRecord, ...] = ()
    review_comments: tuple[CommentRecord, ...] = ()
    issue_comments: tuple[CommentRecord, ...] = ()
    reactions: tuple[ReactionRecord, ...] = ()
    reactions_observable: bool = True
    reactions_detail: str = ""
    checks: tuple[CheckRecord, ...] = ()
    required_checks: tuple[str, ...] = ()
    observed_at: datetime | None = None
    rate_limit_remaining: int | None = None
    notes: tuple[str, ...] = field(default=())


class GitHubClient(Protocol):
    """What the application layer may ask of GitHub."""

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken: ...

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        """The SHA at `refs/heads/<ref>`, or None when the ref does not exist."""
        ...

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None: ...

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef: ...

    def create_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        title: str,
        head_branch: str,
        base_ref: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestRef: ...

    def update_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        base_ref: str | None = None,
    ) -> PullRequestRef: ...

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool = True,
    ) -> Observation: ...

    def reactions_for(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[ReactionRecord, ...]:
        """Reactions on the PR itself and on each review comment and issue comment.

        Raises `UnobservableError` when the App lacks Issues read for the PR-level call."""
        ...

    def workflow_run_logs(
        self, token: InstallationToken, *, repository: str, run_id: str, limit_bytes: int
    ) -> bytes:
        """The available log excerpt for a failed run (Actions read)."""
        ...

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> str:
        """Gated off by configuration on the default path (23): the trigger comment is
        the orchestrator's act under the operator's account, never Crucible's."""
        ...

    def closed_by(self, token: InstallationToken, *, repository: str, number: int) -> str | None:
        """Who closed the pull request, or None when it is not observable."""
        ...

    def delete_ref(self, token: InstallationToken, *, repository: str, ref: str) -> None:
        """Cleanup only; never a branch a task is delivering and never a default branch."""
        ...

    def list_required_checks(
        self, token: InstallationToken, *, repository: str, branch: str
    ) -> Sequence[str]: ...


# ----- the App credential the service owns (ADR 0017) ----------------------------------


class GitHubAppStoreError(Exception):
    """The App credential's store refused a read or a write. The message names the
    store and the status, never a value."""


@dataclass(frozen=True, slots=True)
class AppCredential:
    """The App's id and its private key, read for one signature and dropped. `repr`
    never shows the key."""

    app_id: int
    private_key: bytes = field(repr=False)


class GitHubAppCredentials(Protocol):
    """Where the App credential lives and the one writer of it (ADR 0017).

    On Kubernetes that is the `crucible-github-app` Secret in the service's namespace;
    with the Docker provider it is the files beside `github.app.private_key_path`. A
    credential counts as configured when it has an App id and a key and either the
    service wrote it (the Connect GitHub flow) or `github.enabled` says a deployment
    placed it there on purpose."""

    def describe(self) -> dict[str, Any]:
        """Where it lives and what is there: never a value."""
        ...

    def read(self) -> AppCredential | None:
        """The credential when it is configured, else None. Blocking."""
        ...

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        """Replace the credential whole. Returns what was done, never a value."""
        ...


class GitHubAppDirectory(Protocol):
    """What the App itself can see (crucible#120): its own identity, the accounts it is
    installed on, and the repositories each installation covers. Every token minted to
    list them is discarded before the call returns."""

    def app(self, credential: AppCredential | None = None) -> dict[str, Any]:
        """`GET /app`, signed with `credential` when one is given (to check an id and key
        before they are stored), else with the stored one."""
        ...

    def installations(self) -> list[dict[str, Any]]:
        """`GET /app/installations`: id, account login and type, repository selection."""
        ...

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        """`GET /installation/repositories` under that installation."""
        ...
