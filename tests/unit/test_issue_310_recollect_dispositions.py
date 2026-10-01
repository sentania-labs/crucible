"""Regression coverage for hades #310 recollected feedback dispositions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.application.decisions import record_disposition
from crucible.application.errors import TransitionNotAllowedError
from crucible.application.observation import PHASE_POST_PR, evaluate_delivery_gates
from crucible.contracts.api import DispositionRequest
from crucible.domain.entities import (
    DispositionKind,
    Principal,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    ReviewComment,
    ReviewDisposition,
    Role,
)
from crucible.domain.events import EventKind
from crucible.domain.gates import GateName
from tests.fixtures import FakeClock

OLD_HEAD = "a" * 40
RECOLLECTED_HEAD = "b" * 40
NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


class Rows:
    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = list(rows)

    def list_for_pull_request(self, _pull_request_id: str) -> list[object]:
        return self.rows

    def list_for_task(self, _task_id: str) -> list[object]:
        return self.rows

    def get(self, row_id: str) -> object | None:
        return next((row for row in self.rows if getattr(row, "id", None) == row_id), None)


class Events:
    def __init__(self, decision: object) -> None:
        self.decision = decision
        self.recorded: list[object] = []

    def latest_for_task_kind(self, _task_id: str, kind: str) -> object | None:
        if kind == EventKind.HEAD_DECISION_RECORDED.value:
            return self.decision
        return None

    def append(self, event: object) -> object:
        self.recorded.append(event)
        return event


class Dispositions:
    def __init__(self, disposition: ReviewDisposition) -> None:
        self.disposition = disposition

    def list_for_comments(
        self, comment_ids: list[str], _body_hashes: dict[str, str]
    ) -> list[ReviewDisposition]:
        return [self.disposition] if self.disposition.review_comment_id in comment_ids else []

    def get_by_comment(self, review_comment_id: str, _body_hash: str) -> ReviewDisposition | None:
        if review_comment_id == self.disposition.review_comment_id:
            return self.disposition
        return None


class GateResults:
    def __init__(self) -> None:
        self.recorded: list[object] = []

    def list_for_attempt(self, _attempt_id: str) -> list[object]:
        return []

    def put(self, result: object) -> None:
        self.recorded.append(result)


def scenario() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, ReviewComment]:
    task = SimpleNamespace(id="task", principal_id="foundry", head_sha=RECOLLECTED_HEAD)
    pull_request = SimpleNamespace(
        id="pr", number=310, head_sha=RECOLLECTED_HEAD, state=PullRequestState.OPEN
    )
    comment = ReviewComment(
        id="comment",
        pull_request_id=pull_request.id,
        external_review_id=None,
        github_id="123",
        kind="review_comment",
        login="chatgpt-codex-connector[bot]",
        path="crucible/application/observation.py",
        line=180,
        body="please fix this",
        body_sha256="body-hash",
        created_at=NOW,
        updated_at=NOW,
        reviewed_sha=OLD_HEAD,
    )
    disposition = ReviewDisposition(
        id="disposition",
        review_comment_id=comment.id,
        comment_body_sha256=comment.body_sha256,
        principal_id=task.principal_id,
        disposition=DispositionKind.FIX,
        reasoning="fixed before recollecting",
        created_at=NOW + timedelta(minutes=1),
    )
    heads = [
        PullRequestHead("old", pull_request.id, OLD_HEAD, PushedBy.CRUCIBLE, NOW),
        PullRequestHead(
            "new",
            pull_request.id,
            RECOLLECTED_HEAD,
            PushedBy.OTHER,
            NOW + timedelta(minutes=2),
        ),
    ]
    decision = SimpleNamespace(payload={"action": "recollect", "observed_head": RECOLLECTED_HEAD})
    uow = SimpleNamespace(
        pull_request_heads=Rows(heads),
        events=Events(decision),
        review_comments=Rows([comment]),
        dispositions=Dispositions(disposition),
        review_cycles=Rows([]),
        external_reviews=Rows([]),
        decisions=Rows([]),
        gate_results=GateResults(),
    )
    return uow, task, pull_request, comment


def test_recollected_head_with_every_comment_dispositioned_passes_gate() -> None:
    uow, task, pull_request, comment = scenario()

    result = evaluate_delivery_gates(
        uow,
        FakeClock(NOW + timedelta(minutes=3)),
        task=cast(Any, task),
        attempt_id="attempt",
        pull_request=cast(Any, pull_request),
        policy={
            "gates": {"post_pr": [GateName.FEEDBACK_DISPOSITIONS_COMPLETE.value]},
            "external_review": {"reviewer_logins": [comment.login]},
        },
        certification=None,
        branch_pushed_sha=RECOLLECTED_HEAD,
        phases=(PHASE_POST_PR,),
    )

    assert result["results"][GateName.FEEDBACK_DISPOSITIONS_COMPLETE.value] == "pass"
    assert result["settled_by_correction"] == [comment.id]


def test_duplicate_disposition_is_still_refused() -> None:
    uow, task, pull_request, comment = scenario()
    uow.tasks = SimpleNamespace(get=lambda _task_id: task)
    uow.pull_requests = SimpleNamespace(get_for_task=lambda _task_id: pull_request)
    principal = Principal("foundry", "foundry", Role.ORCHESTRATOR, NOW)

    with pytest.raises(TransitionNotAllowedError, match="already has a disposition"):
        record_disposition(
            uow,
            FakeClock(NOW),
            principal=principal,
            task_id=task.id,
            request=DispositionRequest(
                review_comment_id=comment.id,
                disposition=DispositionKind.FIX,
                reasoning="record it again",
            ),
        )
