"""Hades #337: the supervisor squash-merges exactly the certified head."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from crucible.application.delivery_tick import DeliveryCoordinator, MergePlan
from crucible.domain.entities import PullRequestState
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import (
    GitHubClient,
    GitHubError,
    InstallationToken,
    MergeResult,
    PullRequestRef,
)
from tests.fixtures import FakeClock

HEAD = "a" * 40
MOVED = "b" * 40
MERGE = "c" * 40
NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


class Host:
    def __init__(self, uow: MagicMock) -> None:
        self.uow = uow

    @contextmanager
    def _fenced(self) -> Iterator[MagicMock]:
        yield self.uow

    async def _db(self, fn: Any) -> Any:
        return fn()


class FakeGitHub:
    def __init__(self, *, head: str = HEAD, refusal: GitHubError | None = None) -> None:
        self.head = head
        self.refusal = refusal
        self.merge_calls: list[str] = []

    def installation_token(self, **kwargs: Any) -> InstallationToken:
        return InstallationToken(
            "test-token", expires_at=NOW + timedelta(hours=1), repository="org/repo"
        )

    def get_pull_request(self, token: InstallationToken, **kwargs: Any) -> PullRequestRef:
        return PullRequestRef(
            number=7,
            url="https://github.invalid/org/repo/pull/7",
            head_sha=self.head,
            base_ref="main",
            state="open",
        )

    def merge_pull_request(
        self, token: InstallationToken, *, expected_head_sha: str, **kwargs: Any
    ) -> MergeResult:
        self.merge_calls.append(expected_head_sha)
        if self.refusal is not None:
            raise self.refusal
        return MergeResult(sha=MERGE, merged_at=NOW, merged_by="hades[bot]")


def setup() -> tuple[DeliveryCoordinator, FakeGitHub, MagicMock, Any, MergePlan]:
    task: Any = SimpleNamespace(
        id="task-1",
        external_id="FDY-0267",
        repository_id="repo-1",
        principal_id="principal-1",
        policy_name="default-software",
        policy_version=1,
        state=TaskState.READY_FOR_MERGE,
        head_sha=HEAD,
        updated_at=NOW,
        closed_at=None,
    )
    pull_request = SimpleNamespace(
        id="pr-1",
        task_id=task.id,
        state=PullRequestState.OPEN,
        number=7,
        head_sha=HEAD,
        merge_sha=None,
        merged_at=None,
        merged_by=None,
        base_ref="main",
        observed_head_sha=HEAD,
        observed_base_ref="main",
        mergeable_state="clean",
        merge_refusal_cause=None,
        merge_refusal_head_sha=None,
        merge_refusal_base_ref=None,
        merge_refusal_mergeable_state=None,
        merge_refusal_count=0,
        merge_retry_at=None,
    )
    uow = MagicMock()
    uow.tasks.get.return_value = task
    uow.pull_requests.get.return_value = pull_request
    github = FakeGitHub()
    coordinator = DeliveryCoordinator(Host(uow), FakeClock(NOW), github=cast(GitHubClient, github))
    plan = MergePlan(
        task_id=task.id,
        pull_request_id=pull_request.id,
        number=7,
        repository_name="org/repo",
        installation_id=42,
        certified_head_sha=HEAD,
        base_ref="main",
    )
    return coordinator, github, uow, task, plan


@pytest.mark.asyncio
async def test_certified_head_merges_and_records_github_response() -> None:
    coordinator, github, uow, task, plan = setup()

    assert await coordinator._merge_one(plan) is True

    assert github.merge_calls == [HEAD]
    pull_request = uow.pull_requests.get.return_value
    assert pull_request.state is PullRequestState.MERGED
    assert pull_request.merge_sha == MERGE
    assert pull_request.merged_at == NOW
    assert pull_request.merged_by == "hades[bot]"
    assert task.state is TaskState.MERGED


@pytest.mark.asyncio
async def test_moved_head_is_not_merged() -> None:
    coordinator, github, _uow, task, plan = setup()
    github.head = MOVED

    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []
    assert task.state is TaskState.READY_FOR_MERGE


@pytest.mark.asyncio
async def test_merge_refusal_wakes_with_the_cause() -> None:
    coordinator, github, uow, task, plan = setup()
    github.refusal = GitHubError(409, "base branch has conflicts")

    assert await coordinator._merge_one(plan) is False

    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "ready_for_merge"
    assert "base branch has conflicts" in wake.payload["summary"]
    assert task.state is TaskState.READY_FOR_MERGE


def test_auto_merge_false_leaves_ready_task_alone() -> None:
    coordinator, github, uow, task, _plan = setup()
    uow.tasks.list_by_state.return_value = [task]
    uow.policies.get.return_value = SimpleNamespace(document={"delivery": {"auto_merge": False}})

    assert coordinator._ready_merges() == []
    assert github.merge_calls == []
    assert task.state is TaskState.READY_FOR_MERGE
