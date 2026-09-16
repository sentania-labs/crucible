"""CompletionClaimV1 (11): the worker's report. A claim, never an acceptance.
C1 parses only; gates that consume it are C2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from crucible.contracts.common import StrictModel, check_major_version


class ClaimRefs(StrictModel):
    branch: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    commits: int = Field(ge=0)


class ClaimCheck(StrictModel):
    id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    exit: int
    log: str = Field(min_length=1)


class AcceptanceMapping(StrictModel):
    id: str = Field(min_length=1)
    status: Literal["met", "not_met", "not_exercised", "partial"]
    evidence: str


class ProposedPullRequest(StrictModel):
    title: str = Field(min_length=1)
    body: str
    closes: list[str]


class CompletionClaimV1(StrictModel):
    schema_version: str
    task_external_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    changed_files: list[str]
    refs: ClaimRefs
    checks: list[ClaimCheck]
    acceptance_mapping: list[AcceptanceMapping]
    run_evidence: list[str]
    proposed_pull_request: ProposedPullRequest
    limitations: list[str]
    risks: list[str]
    blockers: list[str]
    follow_ups: list[str]

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)


def parse_claim(document: object) -> tuple[CompletionClaimV1 | None, list[dict[str, Any]]]:
    """Parse a report document. Returns (claim, errors); errors is empty on success."""
    if not isinstance(document, dict):
        return None, [{"loc": [], "msg": "report is not a mapping"}]
    try:
        return CompletionClaimV1.model_validate(document), []
    except ValidationError as exc:
        errors = [
            {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors(include_url=False, include_input=False)
        ]
        return None, errors
