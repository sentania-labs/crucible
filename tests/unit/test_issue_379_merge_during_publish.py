"""Hades #379: a merge or a close seen while a correction publishes is recorded, a new
pull request is never opened in place of the task's own, and the merged head is checked.

The publications run through the delivery coordinator's whole publish step, from the
task in `publishing` to the lookup of the pull request; only GitHub and the publisher
container are stood in for. The store is the in-memory one the #360 tests use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.errors import ContractValidationError
from crucible.application.supervisor import Supervisor
from crucible.application.transitions import move_task, record_event
from crucible.domain.entities import PullRequestState, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import GitHubClient, InstallationToken, PullRequestRef
from crucible.ports.publish import PublishOutcome, PublishRequest
from tests.fixtures import REPOSITORY_URL, FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import (
    MERGE_SHA,
    NEW_HEAD,
    NOW,
    OLD_HEAD,
    PR_ID,
    PR_NUMBER,
    TASK_ID,
    _attach,
    _correction,
    _correction_attempt,
    _GitHub,
    _ready_for_merge,
    _Store,
    _Tasks,
)

MERGED_AT = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)


class _PublishGitHub(_GitHub):
    """The calls a publication makes after the push. `lookup` is what GitHub returns for
    the work branch; opening or updating a pull request is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.remote = NEW_HEAD
        self.lookup: PullRequestRef | None = None
        self.created: list[str] = []
        self.updated: list[int] = []

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        return self.remote

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None:
        return self.lookup

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
    ) -> PullRequestRef:
        self.created.append(head_branch)
        return PullRequestRef(
            number=PR_NUMBER + 1,
            url=f"{REPOSITORY_URL}/pull/{PR_NUMBER + 1}",
            head_sha=NEW_HEAD,
            base_ref=base_ref,
            state="open",
        )

    def update_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        base_ref: str | None = None,
    ) -> PullRequestRef:
        self.updated.append(number)
        raise AssertionError("a pull request that is not open is never updated")


class _Publisher:
    """The publisher container: pushes the bundle's head without force."""

    def __init__(self) -> None:
        self.pushes: list[str] = []

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        self.pushes.append(request.expected_head)
        return PublishOutcome(pushed=True, head_sha=request.expected_head, step="push")

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        return 0


def _merged(head: str) -> PullRequestRef:
    return PullRequestRef(
        number=PR_NUMBER,
        url=f"{REPOSITORY_URL}/pull/{PR_NUMBER}",
        head_sha=head,
        base_ref="main",
        state="closed",
        merged=True,
        merged_at=MERGED_AT,
        merge_commit_sha=MERGE_SHA,
        merged_by="maintainer",
    )


def _closed() -> PullRequestRef:
    return PullRequestRef(
        number=PR_NUMBER,
        url=f"{REPOSITORY_URL}/pull/{PR_NUMBER}",
        head_sha=OLD_HEAD,
        base_ref="main",
        state="closed",
        closed_at=MERGED_AT,
        closed_by="maintainer",
    )


def _supervisor(
    store: _Store, clock: FakeClock, tmp_path: Path, github: _PublishGitHub, publisher: _Publisher
) -> Supervisor:
    supervisor = Supervisor(
        store.uow,
        {"fake": FakeProvider()},
        clock,
        holder="test",
        artifact_store=DiskArtifactStore(tmp_path / "artifacts"),
        github=cast(GitHubClient, github),
        publisher=publisher,
    )
    supervisor.fenced_token = 1
    return supervisor


def _task(store: _Store) -> Task:
    task = store.tasks.get(TASK_ID)
    assert task is not None
    return task


def _correcting(
    tmp_path: Path, *, until: TaskState = TaskState.PUBLISHING
) -> tuple[_Store, FakeClock, Supervisor, _PublishGitHub, _Publisher]:
    """A ready_for_merge correction collected, gated and accepted: the corrected head is
    in `publishing` (or, with `until`, an earlier state of the correction)."""
    store = _ready_for_merge()
    clock = FakeClock(NOW)
    github = _PublishGitHub()
    publisher = _Publisher()
    _attach(store, _correction(), clock)
    supervisor = _supervisor(store, clock, tmp_path, github, publisher)
    supervisor._materialize_scheduled()
    task = _task(store)
    for target, kind in (
        (TaskState.RUNNING, EventKind.TASK_RUNNING),
        (TaskState.REPORTED, EventKind.TASK_REPORTED),
        (TaskState.GATES_PASSED, EventKind.TASK_GATES_PASSED),
        (TaskState.AWAITING_ACCEPTANCE, EventKind.TASK_AWAITING_ACCEPTANCE),
        (TaskState.PUBLISHING, EventKind.TASK_PUBLISHING),
    ):
        if task.state is until:
            break
        move_task(store.uow(), clock, task, target, kind)
        if target is TaskState.REPORTED:
            # Collection binds the task to the corrected head; nothing has pushed it.
            task.head_sha = NEW_HEAD
    return store, clock, supervisor, github, publisher


def _publish(supervisor: Supervisor) -> int:
    return asyncio.run(supervisor.delivery.publish())


def _wakes(store: _Store, reason: str) -> list[str]:
    return [str(w.payload["summary"]) for w in store.wakes.rows if w.reason == reason]


def _merged_event_payload(store: _Store) -> dict[str, object]:
    merged = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_MERGED.value)
    assert merged is not None
    return dict(merged.payload)


# ----- a merge seen by the publication's lookup -------------------------------


def test_a_merge_of_the_pushed_head_during_publishing_is_recorded_and_no_pr_is_opened(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    # The corrected head was pushed and a person merged the PR at that head before the
    # publication looked the PR up.
    github.lookup = _merged(NEW_HEAD)

    assert _publish(supervisor) == 0

    assert publisher.pushes == [NEW_HEAD]
    assert github.created == []
    assert github.updated == []
    task = _task(store)
    assert task.state is TaskState.MERGED
    assert task.head_sha == NEW_HEAD
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    assert pull_request.state is PullRequestState.MERGED
    assert pull_request.merge_sha == MERGE_SHA
    assert pull_request.merged_by == "maintainer"
    payload = _merged_event_payload(store)
    assert payload["merged_from"] == "publishing"
    assert payload["merged_head"] == NEW_HEAD
    assert payload["last_pushed_head"] == NEW_HEAD
    assert payload["merged_head_matches"] is True
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert f"the merged head {NEW_HEAD} is the last head Crucible pushed" in merged[0]
    assert store.escalations.list_for_task(TASK_ID) == []
    failed = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None and failed.payload["pull_request"] == PR_NUMBER
    # Only the first publication completed; the corrected head's did not.
    assert store.events.kinds().count(EventKind.PUBLISH_COMPLETED.value) == 1


def test_a_merged_head_that_is_not_the_pushed_head_is_recorded_and_escalated(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    # The PR was merged at the old head before the corrected head reached the branch.
    github.lookup = _merged(OLD_HEAD)

    assert _publish(supervisor) == 0

    assert github.created == []
    assert _task(store).state is TaskState.MERGED
    payload = _merged_event_payload(store)
    assert payload["merged_head"] == OLD_HEAD
    assert payload["last_pushed_head"] == NEW_HEAD
    assert payload["merged_head_matches"] is False
    escalations = store.escalations.list_for_task(TASK_ID)
    assert len(escalations) == 1
    assert OLD_HEAD in escalations[0].question and NEW_HEAD in escalations[0].question
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert f"the merged head {OLD_HEAD} is not {NEW_HEAD}" in merged[0]


def test_a_closed_pull_request_during_a_correction_fails_the_publication(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    github.lookup = _closed()

    assert _publish(supervisor) == 0

    assert github.created == []
    assert github.updated == []
    assert _task(store).state is TaskState.PUBLISH_FAILED
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    assert pull_request.state is PullRequestState.CLOSED
    assert pull_request.closed_by == "maintainer"
    changed = store.events.latest_for_task_kind(TASK_ID, EventKind.PULL_REQUEST_STATE_CHANGED.value)
    assert changed is not None and changed.payload["state"] == "closed"
    failed = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None
    assert failed.payload["pull_request"] == PR_NUMBER
    assert failed.payload["pull_request_state"] == "closed"
    wakes = _wakes(store, "publish_failed")
    assert len(wakes) == 1
    assert f"pull request #{PR_NUMBER} is closed" in wakes[0]

    # A republish meets the same closed PR and fails again; it never opens a second one.
    task = _task(store)
    move_task(store.uow(), _clock, task, TaskState.PUBLISHING, EventKind.TASK_PUBLISHING)
    assert _publish(supervisor) == 0
    assert github.created == []
    assert _task(store).state is TaskState.PUBLISH_FAILED


def test_a_lookup_that_finds_no_pull_request_does_not_open_a_second_one(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    github.lookup = None

    assert _publish(supervisor) == 0

    assert github.created == []
    assert _task(store).state is TaskState.PUBLISH_FAILED
    wakes = _wakes(store, "publish_failed")
    assert len(wakes) == 1 and f"#{PR_NUMBER}" in wakes[0]


# ----- a merge seen by the poll while publishing or after a failed publish -----


@pytest.mark.parametrize("state", [TaskState.PUBLISHING, TaskState.PUBLISH_FAILED])
def test_a_merge_polled_while_the_corrected_head_publishes_settles_merged(
    tmp_path: Path, state: TaskState
) -> None:
    store, clock, supervisor, github, _publisher = _correcting(tmp_path)
    task = _task(store)
    if state is TaskState.PUBLISH_FAILED:
        move_task(store.uow(), clock, task, state, EventKind.TASK_PUBLISH_FAILED)
    github.merged = True
    clock.advance(300)

    assert asyncio.run(supervisor.delivery.observe()) == 1

    assert github.observed == [PR_NUMBER]
    task = _task(store)
    assert task.state is TaskState.MERGED
    # Nothing pushed the corrected head; the merged task keeps the head on the PR.
    assert task.head_sha == OLD_HEAD
    payload = _merged_event_payload(store)
    assert payload["merged_from"] == state.value
    assert payload["merged_head_matches"] is True
    assert store.escalations.list_for_task(TASK_ID) == []


def test_a_merge_after_collection_leaves_the_last_pushed_head_on_the_task(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, github, _publisher = _correcting(
        tmp_path, until=TaskState.AWAITING_ACCEPTANCE
    )
    assert _task(store).head_sha == NEW_HEAD
    github.merged = True
    clock.advance(300)

    asyncio.run(supervisor.delivery.observe())

    task = _task(store)
    assert task.state is TaskState.MERGED
    assert task.head_sha == OLD_HEAD


# ----- the quota checkpoint after a merge -------------------------------------


def test_a_quota_checkpoint_is_not_pushed_once_the_task_is_merged(tmp_path: Path) -> None:
    store, clock, supervisor, github, publisher = _correcting(tmp_path, until=TaskState.REPORTED)
    _execution, attempt = _correction_attempt(store)
    attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
    task = _task(store)
    task.head_sha = NEW_HEAD
    github.merged = True
    clock.advance(300)
    asyncio.run(supervisor.delivery.observe())
    assert _task(store).state is TaskState.MERGED
    # The checkpoint's own head, as collection would have bound it.
    _task(store).head_sha = NEW_HEAD

    outcome = asyncio.run(supervisor.delivery.push_quota_checkpoint(attempt.id, required=True))

    assert outcome is not None
    pushed, detail = outcome
    assert pushed is False
    assert "merged" in detail
    assert publisher.pushes == []
    assert EventKind.BRANCH_PUSHED.value not in store.events.kinds()


# ----- the gate pass locks only a merged correction ---------------------------


class _LockRecordingTasks(_Tasks):
    def __init__(self, rows: dict[str, Task]) -> None:
        super().__init__()
        self.rows = rows
        self.locked: list[str] = []

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        if for_update:
            self.locked.append(task_id)
        return super().get(task_id)

    def list_by_state(self, state: TaskState, *, for_update: bool = False) -> list[Task]:
        found = super().list_by_state(state)
        if for_update:
            self.locked.extend(t.id for t in found)
        return found


def test_the_gate_pass_locks_a_correction_only_when_its_pull_request_is_merged(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, _github, _publisher = _correcting(tmp_path, until=TaskState.RUNNING)
    tasks = _LockRecordingTasks(store.tasks.rows)
    store.tasks = tasks

    supervisor.delivery._evaluate_gates()

    assert tasks.locked == []
    assert _task(store).state is TaskState.RUNNING

    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.state = PullRequestState.MERGED
    pull_request.merge_sha = MERGE_SHA
    record_event(
        store.uow(),
        _clock,
        EventKind.PULL_REQUEST_STATE_CHANGED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=TASK_ID,
        payload={"pull_request": PR_NUMBER, "state": "merged", "head_sha": OLD_HEAD},
    )

    supervisor.delivery._evaluate_gates()

    assert tasks.locked == [TASK_ID]
    assert _task(store).state is TaskState.MERGED
    assert _merged_event_payload(store)["merged_head_matches"] is True


# ----- which corrections ready_for_merge takes --------------------------------


@pytest.mark.parametrize("reason", ["external_review", "ci_certification", "pre_pr_gates"])
def test_a_ready_for_merge_correction_for_another_reason_is_refused(reason: str) -> None:
    store = _ready_for_merge()
    body = _correction()
    body["correction"]["reason"] = reason

    with pytest.raises(ContractValidationError) as exc:
        _attach(store, body, FakeClock(NOW))

    assert any(e["path"] == "correction.reason" for e in exc.value.errors)
    assert _task(store).state is TaskState.READY_FOR_MERGE


@pytest.mark.parametrize("reason", ["needs_more_work", "internal_review"])
def test_a_ready_for_merge_correction_for_foundrys_own_review_is_taken(reason: str) -> None:
    store = _ready_for_merge()
    body = _correction()
    body["correction"]["reason"] = reason

    task = _attach(store, body, FakeClock(NOW))

    assert task.state is TaskState.SCHEDULED
