"""The task record's push time (hades PR 238 review)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from crucible.application.queries import delivery_view

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
