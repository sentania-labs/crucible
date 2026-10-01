"""The task record's push time (hades PR 238 review)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from crucible.application.queries import attempt_report, delivery_view
from crucible.contracts.api import CompletionClaimView

PUSHED = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
CONFIRMED = datetime(2026, 9, 29, 9, 30, tzinfo=UTC)


def _pushed(ts: datetime, head: str) -> Any:
    return SimpleNamespace(
        kind="branch_pushed", ts=ts, payload={"work_branch": "crucible/T-1", "head_sha": head}
    )


def test_a_republish_that_confirms_the_head_keeps_the_original_push_time() -> None:
    uow: Any = SimpleNamespace(pull_requests=SimpleNamespace(get_for_task=lambda _: None))
    events = [_pushed(PUSHED, "a" * 40), _pushed(CONFIRMED, "a" * 40)]
    view = delivery_view(uow, "T1", events)
    assert view.pushed_head == "a" * 40
    assert view.pushed_at == PUSHED


def test_a_new_head_is_timed_from_its_own_push() -> None:
    uow: Any = SimpleNamespace(pull_requests=SimpleNamespace(get_for_task=lambda _: None))
    events = [_pushed(PUSHED, "a" * 40), _pushed(CONFIRMED, "b" * 40)]
    view = delivery_view(uow, "T1", events)
    assert (view.pushed_head, view.pushed_at) == ("b" * 40, CONFIRMED)


def test_attempt_report_carries_the_fields_crucible_filled_and_the_differences() -> None:
    evidence_rec = SimpleNamespace(
        id=1,
        attempt_id="A1",
        kind="artifact_present",
        source="crucible",
        verified=True,
        payload={
            "role": "completion_claim",
            "filled_by_crucible": ["head_sha", "commits"],
            "differences": [
                {"field": "acceptance_mapping", "detail": "missing status"},
            ],
        },
        observed_at=datetime(2026, 9, 29, tzinfo=UTC),
    )

    uow: Any = SimpleNamespace(
        attempts={"A1": SimpleNamespace(id="A1", state="completed")},
        claims={
            "A1": SimpleNamespace(
                parsed_ok=True,
                parse_errors=[],
                document={"task_external_id": "EX-0001"},
            )
        },
        evidence=SimpleNamespace(
            list_for_attempt=lambda _: [evidence_rec],
        ),
    )

    view = attempt_report(uow, "A1")
    assert isinstance(view, CompletionClaimView)
    assert view.filled_by_crucible == ["head_sha", "commits"]
    assert view.differences == [
        {"field": "acceptance_mapping", "detail": "missing status"},
    ]


def test_attempt_report_defaults_empty_fields_when_no_evidence() -> None:
    uow: Any = SimpleNamespace(
        attempts={"A2": SimpleNamespace(id="A2", state="completed")},
        claims={
            "A2": SimpleNamespace(
                parsed_ok=True,
                parse_errors=[],
                document={"task_external_id": "EX-0002"},
            )
        },
        evidence=SimpleNamespace(
            list_for_attempt=lambda _: [],
        ),
    )

    view = attempt_report(uow, "A2")
    assert isinstance(view, CompletionClaimView)
    assert view.filled_by_crucible == []
    assert view.differences == []
