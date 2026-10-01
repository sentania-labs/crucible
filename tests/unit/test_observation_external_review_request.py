"""The App's review request is publication metadata, not reviewer feedback."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from crucible.application.observation import ObservationResult, record_comments
from crucible.domain.entities import PullRequest, PullRequestState, Task
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import CommentRecord, Observation, PullRequestRef

NOW = datetime(2026, 10, 1, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class Comments:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def get_by_github(self, *_args: object) -> None:
        return None

    def add(self, row: object) -> bool:
        self.rows.append(row)
        return True


def test_trigger_is_ignored_but_same_third_party_comment_is_recorded() -> None:
    trigger_event = SimpleNamespace(
        payload={"comment_id": "request", "comment_login": "foundry-app[bot]"}
    )
    comments = Comments()
    uow = SimpleNamespace(
        events=SimpleNamespace(
            latest_for_task_kind=lambda *_args: trigger_event,
            append=lambda event: event,
        ),
        review_comments=comments,
    )
    task = Task(
        id="task",
        external_id="FDY-0197",
        principal_id="principal",
        project="hades",
        title="title",
        state=TaskState.AWAITING_EXTERNAL_REVIEW,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        repository_id="repository",
        created_at=NOW,
        updated_at=NOW,
    )
    pull_request = PullRequest(
        id="pr",
        task_id=task.id,
        repository_id="repository",
        number=302,
        url="https://example.test/pull/302",
        base_ref="main",
        work_branch="crucible/FDY-0197",
        state=PullRequestState.OPEN,
        head_sha="a" * 40,
        opened_at=NOW,
    )

    def comment(github_id: str, login: str) -> CommentRecord:
        return CommentRecord(
            github_id=github_id,
            login=login,
            body="@codex review",
            created_at=NOW,
            updated_at=NOW,
            kind="issue_comment",
        )

    observation = Observation(
        pull_request=PullRequestRef(
            number=302,
            url=pull_request.url,
            head_sha=pull_request.head_sha,
            base_ref=pull_request.base_ref,
            state="open",
        ),
        issue_comments=(
            comment("request", "foundry-app[bot]"),
            comment("third-party", "someone-else"),
        ),
    )

    signals = record_comments(
        uow,
        Clock(),
        task=task,
        pull_request=pull_request,
        observation=observation,
        allowlist=frozenset(),
        result=ObservationResult(),
        policy={
            "external_review": {
                "provider": "codex",
                "required_rounds": 1,
                "request_on_publish": True,
            }
        },
    )

    assert [row.github_id for row in comments.rows] == ["third-party"]  # type: ignore[attr-defined]
    assert [signal.github_id for signal in signals] == ["third-party"]
