"""TaskContractV1 (05). Intra-document validation lives here; rules that need the
registry (repository, policy, provider) live in the application layer."""

from __future__ import annotations

import hashlib
import json
import posixpath
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version
from crucible.domain.exit_class import ExitClass
from crucible.domain.secrets import find_secrets


class HarnessName(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    AGY = "agy"


class ProviderName(StrEnum):
    FAKE = "fake"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    HOSTPROCESS = "hostprocess"


class RepositoryRef(StrictModel):
    name: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    work_branch: str = Field(min_length=1)


class Scope(StrictModel):
    allowed_paths: list[str] = Field(min_length=1)
    prohibited_paths: list[str]
    may_add_dependencies: bool
    may_modify_ci: bool

    @field_validator("allowed_paths", "prohibited_paths")
    @classmethod
    def _valid_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern or pattern.startswith("/") or pattern != pattern.strip():
                raise ValueError(f"invalid glob {pattern!r}")
            if ".." in posixpath.normpath(pattern).split("/"):
                raise ValueError(f"invalid glob {pattern!r}: parent traversal")
            if pattern.count("[") != pattern.count("]") or pattern.count("{") != pattern.count("}"):
                raise ValueError(f"invalid glob {pattern!r}: unbalanced brackets")
        if len(set(value)) != len(value):
            raise ValueError("duplicate glob")
        return value

    @model_validator(mode="after")
    def _no_full_overlap(self) -> Scope:
        overlap = set(self.allowed_paths) & set(self.prohibited_paths)
        if overlap:
            raise ValueError(f"allowed_paths and prohibited_paths overlap: {sorted(overlap)}")
        return self


class ContextRef(StrictModel):
    kind: Literal["issue", "doc", "pr", "url", "file"]
    ref: str = Field(min_length=1)


class ProjectInstruction(StrictModel):
    kind: Literal["file", "skill"]
    ref: str = Field(min_length=1)


class AcceptanceCriterion(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class CommandVerification(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal["command"] = "command"
    command: str = Field(min_length=1)
    expect_exit: int = 0


class ArtifactVerification(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal["artifact"]
    path: str = Field(min_length=1)


Verification = CommandVerification | ArtifactVerification


class Constraints(StrictModel):
    prohibited_actions: list[str]
    network: Literal["policy", "none"]


class Deliverable(StrictModel):
    kind: Literal["pull_request", "branch", "artifacts"]
    target: str | None = None
    draft: bool = False
    closes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _target_required(self) -> Deliverable:
        if self.kind in ("pull_request", "branch") and not self.target:
            raise ValueError(f"deliverable kind {self.kind} requires target")
        if self.kind == "artifacts" and self.closes:
            raise ValueError("an artifacts deliverable cannot close issues")
        return self


class Reporting(StrictModel):
    report_schema: Literal["CompletionClaimV1"]
    report_dir: str = Field(pattern=r"^/crucible/report$")
    progress_events: bool


class Escalation(StrictModel):
    conditions: list[str]
    action: str = Field(min_length=1)


class PolicyRef(StrictModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class ExecutionRequest(StrictModel):
    harness: HarnessName
    model: str = Field(min_length=1)
    effort: str | None = None
    provider: ProviderName
    image: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    rationale: str = Field(min_length=1)


class Lifecycle(StrictModel):
    max_attempts: int = Field(ge=1)
    retry_on: list[ExitClass]
    cleanup: Literal["policy", "keep", "delete"]

    @field_validator("retry_on")
    @classmethod
    def _retry_classes(cls, value: list[ExitClass]) -> list[ExitClass]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate retry_on entry")
        return value


class CorrectionAddress(StrictModel):
    kind: Literal["review_comment", "ci_finding", "internal_review", "acceptance"]
    id: str
    disposition_id: str | None = None


class Correction(StrictModel):
    of_version: int = Field(ge=1)
    reason: Literal["external_review", "ci_certification", "needs_more_work", "pre_pr_gates"]
    addresses: list[CorrectionAddress]
    instructions: str = Field(min_length=1)
    resume_from: Literal["remote_branch"]
    request_internal_review: bool


class TaskContractV1(StrictModel):
    schema_version: str
    external_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1)
    parent_external_id: str | None
    repository: RepositoryRef
    scope: Scope
    objective: str = Field(min_length=1)
    context: list[ContextRef]
    project_instructions: list[ProjectInstruction]
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    required_verification: list[Verification] = Field(min_length=1)
    constraints: Constraints
    deliverables: list[Deliverable] = Field(min_length=1)
    reporting: Reporting
    escalation: Escalation
    policy: PolicyRef
    execution_request: ExecutionRequest
    lifecycle: Lifecycle
    correction: Correction | None

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _unique_ids(self) -> TaskContractV1:
        ac_ids = [c.id for c in self.acceptance_criteria]
        if len(set(ac_ids)) != len(ac_ids):
            raise ValueError("acceptance_criteria ids must be unique")
        rv_ids = [v.id for v in self.required_verification]
        if len(set(rv_ids)) != len(rv_ids):
            raise ValueError("required_verification ids must be unique")
        return self

    @model_validator(mode="after")
    def _no_secrets(self) -> TaskContractV1:
        matches = find_secrets(self.model_dump(mode="json"))
        if matches:
            first = matches[0]
            raise ValueError(
                f"secret pattern {first.pattern} matched at {first.path}; "
                "contracts never carry credentials"
            )
        return self

    @property
    def verification_commands(self) -> list[str]:
        return [v.command for v in self.required_verification if isinstance(v, CommandVerification)]


def contract_sha256(document: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the document as stored."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
