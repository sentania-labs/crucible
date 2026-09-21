"""A publisher that pushes into the fake GitHub server (18, 23).

It stands in for the container, not for the rules: it asserts that what arrived is a
token the fake actually minted, refuses a push whose remote head moved out of band, and
records every token value it was handed so a test can prove none of them reached the
database, an event, or a wake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from crucible.ports.github import InstallationToken
from crucible.ports.publish import PublishOutcome, PublishRequest
from tests.integration.fake_github import FakeGitHub


@dataclass
class FakePublisher:
    """The `Publisher` port against the fake server's branch table."""

    github: FakeGitHub
    repository: str
    # Set by a test to make the push fail the way an out-of-band remote move does (23),
    # or the way the commit-policy check does before the push is attempted at all.
    refuse_push: str = ""
    refuse_step: str = "push"
    author_problems: tuple[str, ...] = ()
    trailer_problems: tuple[str, ...] = ()
    pushes: list[tuple[str, str]] = field(default_factory=list)
    tokens_seen: list[str] = field(default_factory=list)
    bundle_paths: list[str] = field(default_factory=list)
    bundle_sha256s: list[str] = field(default_factory=list)

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        value = token.reveal()
        self.tokens_seen.append(value)
        self.bundle_paths.append(request.bundle_path)
        self.bundle_sha256s.append(request.bundle_sha256)
        if value not in self.github.tokens:
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="credential",
                detail="the publisher was handed a token the App never minted",
            )
        repo = self.github.repositories[self.repository]
        before = repo.branches.get(request.work_branch, "")
        if self.refuse_push:
            return PublishOutcome(
                pushed=False,
                head_sha=request.expected_head,
                step=self.refuse_step,
                detail=self.refuse_push,
                exit_code=6 if self.refuse_step == "commit-policy" else 5,
                remote_head_before=before,
                author_problems=self.author_problems,
                trailer_problems=self.trailer_problems,
            )
        self.github.push(self.repository, request.work_branch, request.expected_head)
        self.pushes.append((request.work_branch, request.expected_head))
        return PublishOutcome(
            pushed=True,
            head_sha=request.expected_head,
            step="done",
            remote_head_before=before,
        )

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        return 0
