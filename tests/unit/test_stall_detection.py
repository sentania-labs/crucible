from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crucible.application.supervisor import worker_stall_action


def test_stall_thresholds_warn_once_then_fail() -> None:
    activity = datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=299),
            last_activity=activity,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=None,
        )
        is None
    )
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=300),
            last_activity=activity,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=None,
        )
        == "warn"
    )
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=600),
            last_activity=activity,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=activity + timedelta(seconds=300),
        )
        is None
    )
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=1800),
            last_activity=activity,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=activity + timedelta(seconds=300),
        )
        == "fail"
    )


def test_new_activity_allows_a_new_warning() -> None:
    original = datetime(2026, 9, 20, 12, tzinfo=UTC)
    new_activity = original + timedelta(seconds=400)
    assert (
        worker_stall_action(
            now=new_activity + timedelta(seconds=300),
            last_activity=new_activity,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=original + timedelta(seconds=300),
        )
        == "warn"
    )


def test_unverified_progress_suppresses_warning_but_not_failure() -> None:
    activity = datetime(2026, 9, 20, 12, tzinfo=UTC)
    progress = activity + timedelta(seconds=400)
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=600),
            last_activity=activity,
            last_signal=progress,
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=None,
        )
        is None
    )
    assert (
        worker_stall_action(
            now=activity + timedelta(seconds=1800),
            last_activity=activity,
            last_signal=activity + timedelta(seconds=1799),
            warn_seconds=300,
            fail_seconds=1800,
            warned_at=None,
        )
        == "fail"
    )
