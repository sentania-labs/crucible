"""Event kinds (10). Kinds are an enum; adding one is a migration."""

from __future__ import annotations

from enum import StrEnum

PRINCIPAL_CRUCIBLE = "crucible"


class EventKind(StrEnum):
    # task
    TASK_SUBMITTED = "task_submitted"
    TASK_SCHEDULED = "task_scheduled"
    TASK_RUNNING = "task_running"
    TASK_REPORTED = "task_reported"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCEL_REQUESTED = "task_cancel_requested"
    TASK_CANCELLING = "task_cancelling"
    TASK_CANCELLED = "task_cancelled"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TRANSITION_REJECTED = "transition_rejected"
    CONTRACT_REJECTED = "contract_rejected"
    # execution
    EXECUTION_CREATED = "execution_created"
    EXECUTION_ACTIVE = "execution_active"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_CANCELLED = "execution_cancelled"
    # attempt
    ATTEMPT_CREATED = "attempt_created"
    ATTEMPT_PREPARING = "attempt_preparing"
    ATTEMPT_LAUNCHING = "attempt_launching"
    ATTEMPT_RUNNING = "attempt_running"
    ATTEMPT_TIMEOUT_DRAIN = "attempt_timeout_drain"
    ATTEMPT_TIMEOUT_KILL = "attempt_timeout_kill"
    ATTEMPT_CANCEL_KILL = "attempt_cancel_kill"
    ATTEMPT_TERMINATING = "attempt_terminating"
    ATTEMPT_EXITED = "attempt_exited"
    ATTEMPT_LOST = "attempt_lost"
    ATTEMPT_COLLECTED = "attempt_collected"
    ATTEMPT_SUCCEEDED = "attempt_succeeded"
    ATTEMPT_BLOCKED = "attempt_blocked"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_ADOPTED = "attempt_adopted"
    REPORT_PARSED = "report_parsed"
    REPORT_PARSE_FAILED = "report_parse_failed"
    # supervisor
    SUPERVISOR_LEASE_ACQUIRED = "supervisor_lease_acquired"
    SUPERVISOR_LEASE_RELEASED = "supervisor_lease_released"
    ORPHAN_REMOVED = "orphan_removed"
    # principals and configuration
    PRINCIPAL_CREATED = "principal_created"
    REPOSITORY_REGISTERED = "repository_registered"
