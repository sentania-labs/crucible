"""Shared fixture builders (plain functions, no pytest magic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

REPOSITORY_URL = "https://github.com/example-org/example-service"


def contract_document(**overrides: Any) -> dict[str, Any]:
    """A valid TaskContractV1 for the fake provider. Overrides replace top-level keys."""
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "external_id": "EX-0001",
        "title": "Return 409 on duplicate import ID",
        "project": "example-service",
        "parent_external_id": None,
        "repository": {
            "name": "example-service",
            "base_ref": "main",
            "work_branch": "crucible/EX-0001",
        },
        "scope": {
            "allowed_paths": ["src/ledger/**", "tests/ledger/**"],
            "prohibited_paths": [".github/**"],
            "may_add_dependencies": False,
            "may_modify_ci": False,
        },
        "objective": "Importing a duplicate ID must fail with 409 and no partial write.",
        "context": [{"kind": "issue", "ref": f"{REPOSITORY_URL}/issues/17"}],
        "project_instructions": [{"kind": "file", "ref": "CONTRIBUTING.md"}],
        "acceptance_criteria": [
            {"id": "AC1", "text": "Duplicate ID import returns 409."},
            {"id": "AC2", "text": "Existing import tests pass."},
        ],
        "required_verification": [
            {"id": "V1", "command": "make lint", "expect_exit": 0},
            {"id": "V2", "command": "make test", "expect_exit": 0},
            {"id": "V3", "command": "make scan", "expect_exit": 0},
            {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
        ],
        "constraints": {
            "prohibited_actions": ["modify files outside allowed_paths"],
            "network": "policy",
        },
        "deliverables": [
            {
                "kind": "pull_request",
                "target": "main",
                "draft": False,
                "closes": [f"{REPOSITORY_URL}/issues/17"],
            }
        ],
        "reporting": {
            "report_schema": "CompletionClaimV1",
            "report_dir": "/crucible/report",
            "progress_events": True,
        },
        "escalation": {
            "conditions": ["a required verification command does not exist"],
            "action": "write report/blocked.md with the question and exit 75",
        },
        "policy": {"name": "default-software", "version": 2},
        "execution_request": {
            "tier": "standard",
            "harness": "codex",
            "model": "gpt-5.6-luna",
            "effort": "high",
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "timeout_seconds": 3600,
            "rationale": "Mechanical change with clear tests.",
        },
        "lifecycle": {"max_attempts": 2, "retry_on": ["environment", "lost"], "cleanup": "policy"},
        "correction": None,
    }
    doc.update(overrides)
    return doc


class FakeClock:
    """A settable clock. Tests advance it to exercise timeouts and lease expiry."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now
