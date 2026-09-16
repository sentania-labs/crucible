"""ReviewReportV1 (11): the internal non-author review of a collected head."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version


class ReviewerRef(StrictModel):
    kind: Literal["crucible_review_execution", "orchestrator"]
    attempt_id: str | None = None
    principal: str | None = None

    @model_validator(mode="after")
    def _identified(self) -> ReviewerRef:
        if self.kind == "crucible_review_execution" and not self.attempt_id:
            raise ValueError("a crucible_review_execution reviewer must name attempt_id")
        if self.kind == "orchestrator" and not self.principal:
            raise ValueError("an orchestrator reviewer must name principal")
        return self


class ReviewFinding(StrictModel):
    severity: Literal["blocker", "major", "minor", "nit"]
    path: str = Field(min_length=1)
    line: int | None = None
    text: str = Field(min_length=1)


class ReviewReportV1(StrictModel):
    schema_version: str
    task_external_id: str = Field(min_length=1)
    reviewed_head_sha: str = Field(min_length=7, max_length=64)
    reviewer: ReviewerRef
    verdict: Literal["approve", "request_changes"]
    findings: list[ReviewFinding]
    summary: str = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)


def parse_review_report(document: object) -> tuple[ReviewReportV1 | None, list[dict[str, Any]]]:
    """Parse a review document. Returns (report, errors); errors is empty on success."""
    if not isinstance(document, dict):
        return None, [{"loc": [], "msg": "review report is not a mapping"}]
    try:
        return ReviewReportV1.model_validate(document), []
    except ValidationError as exc:
        errors = [
            {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors(include_url=False, include_input=False)
        ]
        return None, errors
