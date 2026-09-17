"""WakeV1 (17): the notification that Foundry's judgment is required.

Rows first, delivery second. `GET /v1/wakes` is the durable fallback that Foundry polls
on every start of session, so a webhook failure only delays."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from crucible.contracts.common import SCHEMA_VERSION, Rfc3339, StrictModel, check_major_version


class WakeReason(StrEnum):
    INTERNAL_REVIEW_NEEDED = "internal_review_needed"
    GATES_PASSED = "gates_passed"
    PRE_PR_GATES_FAILED = "pre_pr_gates_failed"
    BLOCKED = "blocked"
    ATTEMPT_FAILED = "attempt_failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILURE = "auth_failure"
    ESCALATION_STALE = "escalation_stale"
    SUPERVISOR_TAKEOVER = "supervisor_takeover"
    NEEDS_MORE_WORK = "needs_more_work"
    PUBLISH_PENDING = "publish_pending"


class WakeTask(StrictModel):
    id: str
    external_id: str
    state: str


class WakeV1(StrictModel):
    id: str
    schema_version: str = SCHEMA_VERSION
    principal: str
    reason: WakeReason
    task: WakeTask | None = None
    attempt_id: str | None = None
    pull_request: dict[str, Any] | None = None
    summary: str
    links: dict[str, str] = Field(default_factory=dict)
    created_at: Rfc3339

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)
