"""Hades #379: a merge or a close seen while a correction publishes is recorded, a new
pull request is never opened in place of the task's own, and the merged head is checked.

The publications run through the delivery coordinator's whole publish step, from the
task in `publishing` to the lookup of the pull request; only GitHub and the publisher
container are stood in for. The store is the in-memory one the #360 tests use.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.errors import ContractValidationError
from crucible.application.supervisor import Supervisor
from crucible.application.transitions import move_task, record_event
from crucible.domain.entities import CICertification, PullRequestHead, PullRequestState, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import GitHubClient, InstallationToken, Observation, PullRequestRef
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
    _NothingOnThePullRequest,
    _ready_for_merge,
    _Store,
    _Tasks,
)

MERGED_AT = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
# A head someone else pushed to the branch before merging it.
OTHER_HEAD = "d" * 40


class _PublishGitHub(_GitHub):
    """The calls a publication makes. `lookups` are what successive lookups of the work
    branch return (the last one again once the rest are used); `known` is the task's own
    pull request read by its number; `polled`, when set, is what a poll observes.
    Opening or updating a pull request is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.remote = NEW_HEAD
        self.lookups: list[PullRequestRef | None] = [_open(OLD_HEAD)]
        self.known: PullRequestRef = _open(OLD_HEAD)
        self.polled: PullRequestRef | None = None
        self.created: list[str] = []
        self.updated: list[int] = []

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        return self.remote

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None:
        return self.lookups.pop(0) if len(self.lookups) > 1 else self.lookups[0]

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef:
        assert number == PR_NUMBER, "only the task's own pull request is read by number"
        return self.known

    def closed_by(self, token: InstallationToken, *, repository: str, number: int) -> str | None:
        return "maintainer"

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool,
    ) -> Observation:
        if self.polled is None:
            return super().observe(
                token,
                repository=repository,
                number=number,
                base_ref=base_ref,
                with_reactions=with_reactions,
            )
        self.observed.append(number)
        return Observation(pull_request=self.polled)

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
        return _open(NEW_HEAD, number=PR_NUMBER + 1)

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
        return _open(NEW_HEAD, number=number)


class _Publisher:
    """The publisher container: pushes the bundle's head without force. `during_push`,
    when set, runs while the push is under way, as a concurrent poll would."""

    def __init__(self) -> None:
        self.pushes: list[str] = []
        self.outcome: PublishOutcome | None = None
        self.during_push: Callable[[], Awaitable[object] | None] | None = None

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        self.pushes.append(request.expected_head)
        if self.during_push is not None:
            during = self.during_push()
            if inspect.isawaitable(during):
                await during
        if self.outcome is not None:
            return self.outcome
        return PublishOutcome(pushed=True, head_sha=request.expected_head, step="push")

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        return 0


def _open(head: str, *, number: int = PR_NUMBER) -> PullRequestRef:
    return PullRequestRef(
        number=number,
        url=f"{REPOSITORY_URL}/pull/{number}",
        head_sha=head,
        base_ref="main",
        state="open",
    )


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


def _closed(*, closed_by: str | None = "maintainer") -> PullRequestRef:
    return PullRequestRef(
        number=PR_NUMBER,
        url=f"{REPOSITORY_URL}/pull/{PR_NUMBER}",
        head_sha=OLD_HEAD,
        base_ref="main",
        state="closed",
        closed_at=MERGED_AT,
        closed_by=closed_by,
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


def _wake_links(store: _Store, reason: str) -> list[dict[str, str]]:
    return [dict(w.payload.get("links", {})) for w in store.wakes.rows if w.reason == reason]


def _merged_event_payload(store: _Store) -> dict[str, object]:
    merged = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_MERGED.value)
    assert merged is not None
    return dict(merged.payload)


# ----- a merge seen by the publication's lookup -------------------------------


def test_a_merge_before_the_corrected_head_is_pushed_skips_the_push(tmp_path: Path) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    # A person merged the PR at the head Crucible had pushed before the correction; the
    # publication looks the PR up before it pushes anything.
    github.lookups = [_merged(OLD_HEAD)]

    assert _publish(supervisor) == 0

    # The corrected head never reaches the merged branch.
    assert publisher.pushes == []
    assert EventKind.BRANCH_PUSHED.value not in store.events.kinds()
    assert github.created == []
    assert github.updated == []
    task = _task(store)
    assert task.state is TaskState.MERGED
    # The merged task's head is the merged head, not the collected corrected head.
    assert task.head_sha == OLD_HEAD
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    assert pull_request.state is PullRequestState.MERGED
    assert pull_request.merge_sha == MERGE_SHA
    payload = _merged_event_payload(store)
    assert payload["merged_from"] == "publishing"
    assert payload["merged_head"] == OLD_HEAD
    assert payload["last_pushed_head"] == OLD_HEAD
    assert payload["merged_head_matches"] is True
    assert store.escalations.list_for_task(TASK_ID) == []
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert f"the merged head {OLD_HEAD} is a head Crucible pushed" in merged[0]
    assert _wakes(store, "publish_failed") == []


def test_a_merge_of_the_pushed_head_during_publishing_is_recorded_and_no_pr_is_opened(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    # The PR was open when the publication started; the corrected head was pushed and a
    # person merged the PR at that head before the publication looked it up again.
    github.lookups = [_open(OLD_HEAD), _merged(NEW_HEAD)]

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
    assert payload["last_push_was_checkpoint"] is False
    assert payload["merged_head_matches"] is True
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert f"the merged head {NEW_HEAD} is a head Crucible pushed" in merged[0]
    assert store.escalations.list_for_task(TASK_ID) == []
    failed = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None and failed.payload["pull_request"] == PR_NUMBER
    # Only the first publication completed; the corrected head's did not.
    assert store.events.kinds().count(EventKind.PUBLISH_COMPLETED.value) == 1


def test_a_merged_head_that_is_not_the_pushed_head_is_recorded_and_escalated(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    # While the corrected head was pushed, someone else moved the branch and merged it.
    github.lookups = [_open(OLD_HEAD), _merged(OTHER_HEAD)]

    assert _publish(supervisor) == 0

    assert publisher.pushes == [NEW_HEAD]
    assert github.created == []
    assert _task(store).state is TaskState.MERGED
    payload = _merged_event_payload(store)
    assert payload["merged_head"] == OTHER_HEAD
    assert payload["last_pushed_head"] == NEW_HEAD
    assert payload["merged_head_matches"] is False
    escalations = store.escalations.list_for_task(TASK_ID)
    assert len(escalations) == 1
    assert OTHER_HEAD in escalations[0].question
    assert "not among the heads Crucible pushed" in escalations[0].question
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert f"the merged head {OTHER_HEAD} is not among the heads Crucible pushed" in merged[0]


def test_a_poll_winning_the_push_race_refuses_to_record_the_post_merge_push(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, github, publisher = _correcting(tmp_path)
    github.polled = _merged(OLD_HEAD)
    clock.advance(300)

    async def poll_during_push() -> object:
        return await supervisor.delivery.observe()

    publisher.during_push = poll_during_push

    assert _publish(supervisor) == 1

    assert publisher.pushes == [NEW_HEAD]
    assert _task(store).state is TaskState.MERGED
    assert _task(store).head_sha == OLD_HEAD
    assert EventKind.BRANCH_PUSHED.value not in store.events.kinds()
    escalations = store.escalations.list_for_task(TASK_ID)
    assert len(escalations) == 1
    assert NEW_HEAD in escalations[0].question
    assert "after the task left publishing" in escalations[0].question
    merged_wakes = _wakes(store, "merged")
    assert any(f"merged head {OLD_HEAD} is a head Crucible pushed" in wake for wake in merged_wakes)
    assert any(f"head {NEW_HEAD} was pushed after" in wake for wake in merged_wakes)


def test_the_lookup_race_accepts_an_earlier_crucible_head_without_escalation(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    github.lookups = [_open(OLD_HEAD), _merged(OLD_HEAD)]

    assert _publish(supervisor) == 0

    assert publisher.pushes == [NEW_HEAD]
    assert _task(store).state is TaskState.MERGED
    assert _task(store).head_sha == OLD_HEAD
    payload = _merged_event_payload(store)
    assert payload["last_pushed_head"] == NEW_HEAD
    assert payload["merged_head_matches"] is True
    assert store.escalations.list_for_task(TASK_ID) == []


def test_a_closed_pull_request_during_a_correction_fails_the_publication(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    github.lookups = [_closed(closed_by=None)]

    assert _publish(supervisor) == 0

    assert publisher.pushes == []
    assert github.created == []
    assert github.updated == []
    assert _task(store).state is TaskState.PUBLISH_FAILED
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    assert pull_request.state is PullRequestState.CLOSED
    # The lookup carries no closer; it is read from GitHub as a poll reads it.
    assert pull_request.closed_by == "maintainer"
    changed = store.events.latest_for_task_kind(TASK_ID, EventKind.PULL_REQUEST_STATE_CHANGED.value)
    assert changed is not None and changed.payload["state"] == "closed"
    failed = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None
    assert failed.payload["pull_request"] == PR_NUMBER
    assert failed.payload["pull_request_state"] == "closed"
    wakes = _wakes(store, "publish_failed")
    assert len(wakes) == 1
    assert f"pull request #{PR_NUMBER} is closed by maintainer" in wakes[0]
    # A republish can only meet the same closed PR, so none is offered.
    assert "republish" not in _wake_links(store, "publish_failed")[0]
    assert "a republish would fail the same way" in wakes[0]

    # A republish forced anyway meets the same closed PR and fails again; it never opens
    # a second one.
    task = _task(store)
    move_task(store.uow(), _clock, task, TaskState.PUBLISHING, EventKind.TASK_PUBLISHING)
    assert _publish(supervisor) == 0
    assert github.created == []
    assert publisher.pushes == []
    assert _task(store).state is TaskState.PUBLISH_FAILED


def test_a_lookup_that_finds_no_pull_request_does_not_open_a_second_one(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    github.lookups = [None]
    github.known = _closed()

    assert _publish(supervisor) == 0

    assert publisher.pushes == []
    assert github.created == []
    assert _task(store).state is TaskState.PUBLISH_FAILED
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None and pull_request.state is PullRequestState.CLOSED
    wakes = _wakes(store, "publish_failed")
    assert len(wakes) == 1 and f"#{PR_NUMBER}" in wakes[0]


def test_another_open_pull_request_on_the_branch_is_not_adopted(tmp_path: Path) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    # The task's PR was closed and someone opened a new one from the same branch.
    github.lookups = [_open(OLD_HEAD, number=PR_NUMBER + 1)]
    github.known = _closed()
    cycles_before = list(store.review_cycles.rows)

    assert _publish(supervisor) == 0

    assert publisher.pushes == []
    assert github.created == []
    assert github.updated == []
    assert _task(store).state is TaskState.PUBLISH_FAILED
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    # The task's own PR record keeps its number and records the close.
    assert pull_request.number == PR_NUMBER
    assert pull_request.state is PullRequestState.CLOSED
    assert pull_request.closed_by == "maintainer"
    # The counted external round stays on the task's PR; nothing is inherited.
    assert store.review_cycles.rows == cycles_before
    failed = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None
    assert failed.payload["pull_request"] == PR_NUMBER
    assert failed.payload["other_pull_request"] == PR_NUMBER + 1
    wakes = _wakes(store, "publish_failed")
    assert len(wakes) == 1
    assert f"#{PR_NUMBER + 1}" in wakes[0] and "not adopted" in wakes[0]


def test_the_tasks_open_pull_request_wins_over_another_open_pr_on_the_branch(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    github.known = _open(OLD_HEAD)
    github.lookups = [_open(OLD_HEAD, number=PR_NUMBER + 1)]
    store.pull_request_heads = _Heads()  # type: ignore[assignment]

    assert _publish(supervisor) == 1

    assert publisher.pushes == [NEW_HEAD]
    assert github.created == []
    assert github.updated == [PR_NUMBER]
    assert _task(store).state is not TaskState.PUBLISH_FAILED


def test_another_pull_request_on_the_branch_with_the_own_one_merged_settles_merged(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github, publisher = _correcting(tmp_path)
    github.lookups = [_open(OLD_HEAD, number=PR_NUMBER + 1)]
    github.known = _merged(OLD_HEAD)

    assert _publish(supervisor) == 0

    assert publisher.pushes == []
    assert github.updated == []
    task = _task(store)
    assert task.state is TaskState.MERGED
    assert task.head_sha == OLD_HEAD
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None and pull_request.number == PR_NUMBER
    assert pull_request.state is PullRequestState.MERGED
    assert store.escalations.list_for_task(TASK_ID) == []


def test_a_publication_failure_after_a_merge_settled_the_task_wakes_nobody(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, _github, publisher = _correcting(tmp_path)

    def poll_settles_merged() -> None:
        # A poll observes the merge while the publisher container is pushing.
        move_task(store.uow(), clock, _task(store), TaskState.MERGED, EventKind.TASK_MERGED)

    publisher.during_push = poll_settles_merged
    publisher.outcome = PublishOutcome(
        pushed=False, head_sha=NEW_HEAD, step="push", detail="the push was rejected"
    )

    assert _publish(supervisor) == 0

    assert _task(store).state is TaskState.MERGED
    assert _wakes(store, "publish_failed") == []
    assert EventKind.TASK_PUBLISH_FAILED.value not in store.events.kinds()


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


@pytest.mark.parametrize("by", ["maintainer", None])
def test_a_close_polled_after_the_corrected_heads_publication_failed_is_recorded(
    tmp_path: Path, by: str | None
) -> None:
    store, clock, supervisor, github, _publisher = _correcting(tmp_path)
    move_task(
        store.uow(), clock, _task(store), TaskState.PUBLISH_FAILED, EventKind.TASK_PUBLISH_FAILED
    )
    github.polled = _closed(closed_by=by)
    clock.advance(300)

    assert asyncio.run(supervisor.delivery.observe()) == 1

    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    assert pull_request.state is PullRequestState.CLOSED
    changed = store.events.latest_for_task_kind(TASK_ID, EventKind.PULL_REQUEST_STATE_CHANGED.value)
    assert changed is not None and changed.payload["state"] == "closed"
    assert _task(store).state is TaskState.PUBLISH_FAILED
    wakes = _wakes(store, "pull_request_closed")
    assert len(wakes) == 1
    assert f"pull request #{PR_NUMBER} was closed without being merged" in wakes[0]
    assert "publish_failed" in wakes[0]
    assert "cancel it, or reopen the pull request and then republish" in wakes[0]

    # The closed PR is not polled again, and the wake is not repeated.
    clock.advance(3600)
    asyncio.run(supervisor.delivery.observe())
    assert github.observed == [PR_NUMBER]
    assert len(_wakes(store, "pull_request_closed")) == 1


def test_a_close_while_a_correction_runs_explains_reopen_or_cancel(tmp_path: Path) -> None:
    store, clock, supervisor, github, _publisher = _correcting(tmp_path, until=TaskState.RUNNING)
    github.polled = _closed()
    clock.advance(300)

    assert asyncio.run(supervisor.delivery.observe()) == 1

    assert _task(store).state is TaskState.RUNNING
    wakes = _wakes(store, "pull_request_closed")
    assert len(wakes) == 1
    assert "the correction continues" in wakes[0]
    assert "publication will fail unless the pull request is reopened" in wakes[0]
    assert "cancelling the task is the usual answer" in wakes[0]


class _Certifications:
    def __init__(self) -> None:
        self.rows: list[CICertification] = []

    def get_for_head(self, pull_request_id: str, head_sha: str) -> CICertification | None:
        return next(
            (
                c
                for c in reversed(self.rows)
                if (c.pull_request_id, c.head_sha) == (pull_request_id, head_sha)
            ),
            None,
        )

    def put(self, certification: CICertification) -> CICertification:
        self.rows.append(certification)
        return certification


class _Heads:
    def __init__(self) -> None:
        self.rows: list[PullRequestHead] = []

    def add(self, head: PullRequestHead) -> None:
        self.rows.append(head)

    def list_for_pull_request(self, pull_request_id: str) -> list[PullRequestHead]:
        return [h for h in self.rows if h.pull_request_id == pull_request_id]


def _first_publication_failed(
    tmp_path: Path,
) -> tuple[_Store, FakeClock, Supervisor, _PublishGitHub]:
    """A first publication that opened the PR and then failed: nothing was published to
    the task before, and no correction is under way."""
    store, clock, supervisor, github, _publisher = _correcting(tmp_path)
    store.events.rows = [
        e for e in store.events.rows if e.kind != EventKind.PUBLISH_COMPLETED.value
    ]
    store.ci_certifications = _Certifications()  # type: ignore[attr-defined]
    store.reactions = _NothingOnThePullRequest()  # type: ignore[attr-defined]
    store.pull_request_heads = _Heads()  # type: ignore[assignment]
    task = _task(store)
    task.head_sha = OLD_HEAD
    move_task(store.uow(), clock, task, TaskState.PUBLISH_FAILED, EventKind.TASK_PUBLISH_FAILED)
    clock.advance(300)
    return store, clock, supervisor, github


def test_a_first_publications_pr_is_polled_normally_after_its_publication_failed(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, github = _first_publication_failed(tmp_path)

    assert asyncio.run(supervisor.delivery.observe()) == 1

    assert github.observed == [PR_NUMBER]
    polled = store.events.latest_for_task_kind(TASK_ID, EventKind.PULL_REQUEST_POLLED.value)
    assert polled is not None
    # The whole poll: reviews, comments and checks are counted, not only the merge.
    assert "during_correction" not in polled.payload
    assert "reviews" in polled.payload and "checks" in polled.payload
    assert _task(store).state is TaskState.PUBLISH_FAILED


def test_a_merge_of_a_first_publications_pr_is_a_plain_early_merge(tmp_path: Path) -> None:
    store, _clock, supervisor, github = _first_publication_failed(tmp_path)
    # Even a merged head Crucible did not push is no correction's business here.
    github.polled = _merged(OTHER_HEAD)

    asyncio.run(supervisor.delivery.observe())

    task = _task(store)
    assert task.state is TaskState.MERGED
    payload = _merged_event_payload(store)
    assert payload["merged_from"] == "publish_failed"
    assert "merged_head_matches" not in payload
    assert store.escalations.list_for_task(TASK_ID) == []
    merged = _wakes(store, "merged")
    assert len(merged) == 1
    assert "before Crucible saw it ready for merge" in merged[0]
    assert "correction" not in merged[0]


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


def test_a_merge_of_a_quota_checkpoint_is_escalated_and_named(tmp_path: Path) -> None:
    store, clock, supervisor, github, publisher = _correcting(tmp_path, until=TaskState.REPORTED)
    _execution, attempt = _correction_attempt(store)
    attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
    _task(store).head_sha = NEW_HEAD

    outcome = asyncio.run(supervisor.delivery.push_quota_checkpoint(attempt.id, required=True))

    assert outcome == (True, "checkpoint pushed")
    assert publisher.pushes == [NEW_HEAD]
    pushed = store.events.latest_for_task_kind(TASK_ID, EventKind.BRANCH_PUSHED.value)
    assert pushed is not None and pushed.payload["checkpoint"] is True

    # A person merges the partial, ungated checkpoint head.
    github.polled = _merged(NEW_HEAD)
    clock.advance(300)
    asyncio.run(supervisor.delivery.observe())

    task = _task(store)
    assert task.state is TaskState.MERGED
    assert task.head_sha == NEW_HEAD
    payload = _merged_event_payload(store)
    assert payload["merged_head_matches"] is True
    assert payload["last_push_was_checkpoint"] is True
    escalations = store.escalations.list_for_task(TASK_ID)
    assert len(escalations) == 1
    assert "quota checkpoint" in escalations[0].question
    assert "passed no gate" in escalations[0].question
    merged = _wakes(store, "merged")
    assert len(merged) == 1 and "quota checkpoint" in merged[0]


def test_an_unseen_merge_of_an_earlier_pushed_head_ignores_a_later_checkpoint(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, github, publisher = _correcting(tmp_path, until=TaskState.REPORTED)
    _execution, attempt = _correction_attempt(store)
    attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
    _task(store).head_sha = NEW_HEAD

    assert asyncio.run(supervisor.delivery.push_quota_checkpoint(attempt.id, required=True)) == (
        True,
        "checkpoint pushed",
    )

    github.polled = _merged(OLD_HEAD)
    clock.advance(300)
    asyncio.run(supervisor.delivery.observe())

    assert publisher.pushes == [NEW_HEAD]
    assert _task(store).state is TaskState.MERGED
    assert _task(store).head_sha == OLD_HEAD
    assert _merged_event_payload(store)["merged_head_matches"] is True
    assert store.escalations.list_for_task(TASK_ID) == []


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
