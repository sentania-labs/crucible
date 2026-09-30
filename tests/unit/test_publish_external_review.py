"""Publication requests a configured external review exactly once per pull request."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from crucible.application.publish import (
    PublishPlan,
    external_review_request_exists,
    external_review_trigger,
    request_external_review,
)
from crucible.ports.github import CommentRecord, InstallationToken


def _policy(**review: object) -> dict[str, object]:
    return {
        "external_review": {
            "provider": "codex",
            "required_rounds": 1,
            "request_on_publish": True,
            **review,
        }
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[CommentRecord] = []
        self.posts: list[str] = []

    def issue_comments(self, *_args: object, **_kwargs: object) -> tuple[CommentRecord, ...]:
        return tuple(self.comments)

    def post_issue_comment(self, *_args: object, **kwargs: object) -> CommentRecord:
        self.posts.append(str(kwargs["body"]))
        now = datetime(2026, 9, 30, tzinfo=UTC)
        comment = CommentRecord(
            github_id="42",
            login="crucible-spike[bot]",
            body=str(kwargs["body"]),
            created_at=now,
            updated_at=now,
            kind="issue_comment",
        )
        self.comments.append(comment)
        return comment


def _plan(policy: dict[str, object]) -> PublishPlan:
    return PublishPlan(
        task_id="T1",
        external_id="EX-1",
        principal_id="P1",
        attempt_id="A1",
        head_sha="a" * 40,
        repository_id="R1",
        repository_name="owner/repo",
        push_url="https://github.com/owner/repo.git",
        installation_id=1,
        base_ref="main",
        work_branch="crucible/EX-1",
        deliverable_kind="pull_request",
        draft=False,
        image="worker:test",
        bundle_path="/tmp/work.bundle",
        bundle_sha256="b" * 64,
        policy=policy,
    )


def _token() -> InstallationToken:
    return InstallationToken(
        "token", expires_at=datetime(2030, 1, 1, tzinfo=UTC), repository="owner/repo"
    )


def test_publish_posts_the_configured_external_review_once() -> None:
    assert external_review_trigger(_policy()) == "@codex review"
    assert external_review_trigger(_policy(trigger_comment="@reviewer please review")) == (
        "@reviewer please review"
    )
    github = FakeGitHub()
    plan = _plan(_policy())
    comment = request_external_review(
        github,  # type: ignore[arg-type]
        _token(),
        plan=plan,
        pull_request_number=7,
        previous=None,
    )
    assert comment is not None
    previous = SimpleNamespace(
        payload={
            "pull_request": 7,
            "comment_id": comment.github_id,
            "comment_login": comment.login,
        }
    )
    assert (
        request_external_review(
            github,  # type: ignore[arg-type]
            _token(),
            plan=plan,
            pull_request_number=7,
            previous=previous,
        )
        is None
    )
    assert github.posts == ["@codex review"]


def test_publish_does_not_request_review_when_turned_off_or_provider_is_absent() -> None:
    assert external_review_trigger(_policy(request_on_publish=False)) is None
    assert external_review_trigger(_policy(provider=None)) is None
    github = FakeGitHub()
    for policy in (_policy(request_on_publish=False), _policy(provider=None)):
        assert (
            request_external_review(
                github,  # type: ignore[arg-type]
                _token(),
                plan=_plan(policy),
                pull_request_number=7,
                previous=None,
            )
            is None
        )
    assert github.posts == []


def test_republish_does_not_request_the_apps_trigger_again() -> None:
    now = datetime(2026, 9, 30, tzinfo=UTC)
    comment = CommentRecord(
        github_id="42",
        login="crucible-spike[bot]",
        body="@codex review",
        created_at=now,
        updated_at=now,
        kind="issue_comment",
    )
    previous = SimpleNamespace(
        payload={
            "pull_request": 7,
            "comment_id": "42",
            "comment_login": "crucible-spike[bot]",
        }
    )
    assert external_review_request_exists(previous, (comment,), "@codex review", 7)
