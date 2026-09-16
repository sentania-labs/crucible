from __future__ import annotations

from crucible.domain.exit_class import ExitClass, classify_exit


def test_exit_zero_with_report_is_completed() -> None:
    assert (
        classify_exit(exit_code=0, report_present=True, blocked_present=False)
        is ExitClass.COMPLETED
    )


def test_exit_zero_without_report_is_never_success() -> None:
    assert (
        classify_exit(exit_code=0, report_present=False, blocked_present=False)
        is ExitClass.COMPLETED_WITHOUT_REPORT
    )


def test_exit_75_needs_blocked_md() -> None:
    assert (
        classify_exit(exit_code=75, report_present=False, blocked_present=True) is ExitClass.BLOCKED
    )
    assert (
        classify_exit(exit_code=75, report_present=False, blocked_present=False)
        is ExitClass.CRASHED
    )


def test_environment_and_crash() -> None:
    assert (
        classify_exit(exit_code=70, report_present=False, blocked_present=False)
        is ExitClass.ENVIRONMENT
    )
    assert (
        classify_exit(exit_code=1, report_present=False, blocked_present=False) is ExitClass.CRASHED
    )


def test_loss_timeout_kill_precedence() -> None:
    assert (
        classify_exit(exit_code=0, report_present=True, blocked_present=False, lost=True)
        is ExitClass.LOST
    )
    assert (
        classify_exit(exit_code=137, report_present=False, blocked_present=False, timed_out=True)
        is ExitClass.TIMEOUT
    )
    assert (
        classify_exit(exit_code=137, report_present=False, blocked_present=False, killed=True)
        is ExitClass.KILLED
    )
    assert (
        classify_exit(exit_code=None, report_present=False, blocked_present=False)
        is ExitClass.UNKNOWN
    )
