"""A re-run decision recorded against failures stored before FDY-0139 (hades #232)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from crucible.application.observation import pending_rerun
from crucible.domain.entities import CIAction

DECIDED_AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _uow(stale_failures: list[dict[str, Any]]) -> Any:
    event = SimpleNamespace(
        ts=DECIDED_AT,
        payload={
            "action": CIAction.RERUN.value,
            "head_sha": "abc",
            "ci_decision_id": "D1",
            "stale_failures": stale_failures,
        },
    )
    return SimpleNamespace(events=SimpleNamespace(latest_for_task_kind=lambda *_: event))


def test_legacy_rows_without_run_ids_fall_back_to_the_decision_time() -> None:
    uow = _uow([{"run_id": None, "completed_at": None}])
    task: Any = SimpleNamespace(id="T1")
    # The next poll sees the same old failure with the run id GitHub reports today; it
    # concluded before the decision, so it is still the one the re-run was about.
    earlier: list[tuple[str, str | None]] = [("7001", "2026-09-29T11:00:00+00:00")]
    assert pending_rerun(uow, task, head_sha="abc", failures=earlier) == "D1"
    # A failure that concluded after the decision is a new one.
    later: list[tuple[str, str | None]] = [("7002", "2026-09-29T13:00:00+00:00")]
    assert pending_rerun(uow, task, head_sha="abc", failures=later) is None


def test_listed_run_ids_still_decide() -> None:
    uow = _uow([{"run_id": "7001", "completed_at": "2026-09-29T11:00:00+00:00"}])
    task: Any = SimpleNamespace(id="T1")
    stale: list[tuple[str, str | None]] = [("7001", "2026-09-29T11:00:00+00:00")]
    assert pending_rerun(uow, task, head_sha="abc", failures=stale) == "D1"
    assert pending_rerun(uow, task, head_sha="abc", failures=[("7002", None)]) is None
