"""ExitClass: the one enum contracts, policies, and failure semantics share (07, 16)."""

from __future__ import annotations

from enum import StrEnum

EXIT_CODE_BLOCKED = 75
EXIT_CODE_ENVIRONMENT = 70


class ExitClass(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITHOUT_REPORT = "completed_without_report"
    # The harness exited while its own tooling reported a command still running (issue
    # 128): never a clean completion, whatever the exit code and the report say.
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    ENVIRONMENT = "environment"
    AUTH_FAILURE = "auth_failure"
    PROVIDER_ERROR = "provider_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    KILLED = "killed"
    CRASHED = "crashed"
    LOST = "lost"
    UNKNOWN = "unknown"


def classify_exit(
    *,
    exit_code: int | None,
    report_present: bool,
    blocked_present: bool,
    lost: bool = False,
    timed_out: bool = False,
    killed: bool = False,
) -> ExitClass:
    """Deterministic classification of an attempt's exit (07 report parsing, 16 table).

    Precedence: loss, then a termination Crucible itself performed, then the code.
    """
    if lost:
        return ExitClass.LOST
    if timed_out:
        return ExitClass.TIMEOUT
    if killed:
        return ExitClass.KILLED
    if exit_code is None:
        return ExitClass.UNKNOWN
    if exit_code == 0:
        return ExitClass.COMPLETED if report_present else ExitClass.COMPLETED_WITHOUT_REPORT
    if exit_code == EXIT_CODE_BLOCKED:
        # exit 75 without blocked.md is a plain failure (07)
        return ExitClass.BLOCKED if blocked_present else ExitClass.CRASHED
    if exit_code == EXIT_CODE_ENVIRONMENT:
        return ExitClass.ENVIRONMENT
    return ExitClass.CRASHED
