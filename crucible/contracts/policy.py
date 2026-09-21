"""PolicyV1 and RoutingPolicyV1 (05b).

Every tunable the specification mentions lives in a policy document, so nothing is a
magic default in code. Validation here is the intra-document part; the rules that need
a principal (operator-only fields) live in the application layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version
from crucible.domain.exit_class import ExitClass
from crucible.domain.gates import (
    ALL_GATES,
    POST_PR_GATES,
    PRE_PR_GATES,
    PUBLICATION_GATES,
)

# 05b: these two may only be set true by an operator or admin principal.
OPERATOR_ONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("ci_certification", "allow_no_ci"),
    ("deliverables", "allow_branch_only"),
    ("release", "require_operator_approval"),
)


class Bounds(StrictModel):
    min: int = Field(ge=1)
    max: int = Field(ge=1)
    default: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Bounds:
        if not self.min <= self.default <= self.max:
            raise ValueError("timeout_seconds must satisfy min <= default <= max")
        return self


class AttemptCap(StrictModel):
    max: int = Field(ge=1)
    default: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> AttemptCap:
        if self.default > self.max:
            raise ValueError("max_attempts default exceeds max")
        return self


class Limits(StrictModel):
    timeout_seconds: Bounds
    max_attempts: AttemptCap
    grace_seconds: int = Field(ge=0)
    stall_warn_seconds: int = Field(ge=1)
    stall_fail_seconds: int = Field(ge=1)
    auth_retry_delay_seconds: int = Field(ge=0)
    escalation_stale_hours: int = Field(ge=1)
    wake_retry_hours: int = Field(ge=1)

    @model_validator(mode="after")
    def _stall_order(self) -> Limits:
        if self.stall_warn_seconds > self.stall_fail_seconds:
            raise ValueError("stall_warn_seconds must not exceed stall_fail_seconds")
        return self


class Retry(StrictModel):
    eligible_classes: list[ExitClass]
    auth_failure_max: int = Field(ge=0)

    @field_validator("eligible_classes")
    @classmethod
    def _unique(cls, value: list[ExitClass]) -> list[ExitClass]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate entry in retry.eligible_classes")
        return value


class Concurrency(StrictModel):
    per_provider: int = Field(ge=1)
    per_harness: dict[str, int]

    @field_validator("per_harness")
    @classmethod
    def _positive(cls, value: dict[str, int]) -> dict[str, int]:
        for harness, limit in value.items():
            if limit < 1:
                raise ValueError(f"concurrency.per_harness.{harness} must be at least 1")
        return value


class Resources(StrictModel):
    cpus: int = Field(ge=1)
    memory: str = Field(min_length=1)
    pids: int = Field(ge=1)
    tmpfs_total: str = Field(min_length=1)


class Network(StrictModel):
    mode: Literal["egress-proxy", "none"]
    egress_allowlist: list[str]
    harness_endpoints: str = Field(min_length=1)

    @field_validator("egress_allowlist")
    @classmethod
    def _hostnames(cls, value: list[str]) -> list[str]:
        for host in value:
            if "*" in host or "/" in host or not host.strip():
                raise ValueError(f"egress_allowlist entry {host!r} must be a bare hostname")
        return value


class NamedVersion(StrictModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class RoutingRef(StrictModel):
    policy: NamedVersion


class Images(StrictModel):
    allowlist: list[str] = Field(min_length=1)
    require_default_or_retained: bool


class Git(StrictModel):
    author_name: str = Field(min_length=1)
    author_email: str = Field(min_length=1)
    commit_trailer: str = Field(min_length=1)
    work_branch_pattern: str = Field(min_length=1)
    protected_branches: list[str]


class RepositoryRules(StrictModel):
    required_checks: list[str]


class Gates(StrictModel):
    pre_pr: list[str]
    publication: list[str]
    post_pr: list[str]
    skipped: list[str]

    @model_validator(mode="after")
    def _partition(self) -> Gates:
        groups = (self.pre_pr, self.publication, self.post_pr, self.skipped)
        listed = [gate for group in groups for gate in group]
        unknown = sorted(set(listed) - ALL_GATES)
        if unknown:
            raise ValueError(f"unknown gates: {unknown}")
        if len(listed) != len(set(listed)):
            raise ValueError("a gate appears in more than one group")
        missing = sorted(ALL_GATES - set(listed))
        if missing:
            raise ValueError(f"gates in no group: {missing}")
        for group_name, group, expected in (
            ("pre_pr", self.pre_pr, PRE_PR_GATES),
            ("publication", self.publication, PUBLICATION_GATES),
            ("post_pr", self.post_pr, POST_PR_GATES),
        ):
            stray = sorted(set(group) - expected)
            if stray:
                raise ValueError(f"gates.{group_name} lists gates from another phase: {stray}")
        return self


class Deliverables(StrictModel):
    allow_branch_only: bool
    on_out_of_band_head: Literal["block"]


class PullRequestRules(StrictModel):
    require_pre_pr_verification: bool
    open_only_after_pre_pr_gates_pass: bool
    publish_requires_acceptance: bool
    title_from: Literal["claim"]
    body_template: Literal["default"]
    closing_refs: Literal["contract_only"]


class InternalReview(StrictModel):
    required: bool
    required_for_corrections: bool
    reviewer_must_not_be_author: bool
    executor: Literal["orchestrator_or_crucible", "orchestrator", "crucible"]


class ExternalReview(StrictModel):
    provider: str = Field(min_length=1)
    reviewer_logins: list[str]
    required_rounds: int = Field(ge=0)
    retrigger_after_correction: bool
    require_review_on_final_sha: bool
    require_feedback_disposition: bool
    accepted_signals: list[str]
    components: list[str] = Field(default_factory=lambda: ["code"])
    round_counting: str = Field(min_length=1)
    wait_timeout_hours: int = Field(ge=1)

    @model_validator(mode="after")
    def _logins_when_required(self) -> ExternalReview:
        if self.required_rounds > 0 and not self.reviewer_logins:
            raise ValueError("reviewer_logins must be non-empty when required_rounds is above 0")
        return self


class CiCertification(StrictModel):
    require_green_on_final_sha: bool
    required_checks: list[str]
    allow_no_ci: bool
    on_failure: Literal["escalate"]
    automatic_retry: bool
    automatic_worker_correction: bool
    wait_timeout_hours: int = Field(ge=1)


class ReleaseRules(StrictModel):
    require_operator_approval: bool
    authorization_recorder: Literal["orchestrator_relay", "operator_token"]
    trigger: Literal["tag"]
    tag_pattern: str = Field(min_length=1)
    version_files: list[str]
    changelog_required: bool


class Cleanup(StrictModel):
    workspace_on_success: Literal["keep_diff_only", "keep", "delete"]
    workspace_on_failure: Literal["keep_diff_only", "keep", "delete"]
    container_remove: Literal["always", "never"]
    credential_volume_remove: str = Field(min_length=1)


class Retention(StrictModel):
    logs_and_transcripts_days: int = Field(ge=1)
    bootstrap_archive_days: int = Field(ge=1)
    completed_workspaces_days: int = Field(ge=1)
    wakes_after_ack_days: int = Field(ge=1)
    indefinite: list[str]


class PolicyV1(StrictModel):
    schema_version: str
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    description: str = Field(min_length=1)
    limits: Limits
    retry: Retry
    concurrency: Concurrency
    resources: Resources
    network: Network
    routing: RoutingRef
    images: Images
    git: Git
    repository: RepositoryRules
    gates: Gates
    deliverables: Deliverables
    pull_request: PullRequestRules
    internal_review: InternalReview
    external_review: ExternalReview
    ci_certification: CiCertification
    release: ReleaseRules
    cleanup: Cleanup
    retention: Retention

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _external_rounds_zero_skips_gates(self) -> PolicyV1:
        if self.external_review.required_rounds == 0:
            for gate in ("external_review_rounds", "feedback_dispositions_complete"):
                if gate not in self.gates.skipped:
                    raise ValueError(
                        f"required_rounds is 0, so {gate} must be listed in gates.skipped"
                    )
        return self

    def operator_only_settings(self) -> list[str]:
        """The 05b fields whose current value only an operator or admin may upload."""
        out: list[str] = []
        if self.ci_certification.allow_no_ci:
            out.append("ci_certification.allow_no_ci")
        if self.deliverables.allow_branch_only:
            out.append("deliverables.allow_branch_only")
        if not self.release.require_operator_approval:
            out.append("release.require_operator_approval")
        return out


class RoutingTier(StrictModel):
    allowed_capability: list[Literal["small", "mid", "frontier"]] = Field(min_length=1)
    prefer: list[Literal["small", "mid", "frontier"]] = Field(min_length=1)

    @model_validator(mode="after")
    def _prefer_subset(self) -> RoutingTier:
        stray = sorted(set(self.prefer) - set(self.allowed_capability))
        if stray:
            raise ValueError(f"prefer lists capabilities the tier does not allow: {stray}")
        return self


class RoutingModel(StrictModel):
    id: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    endpoint: Literal["subscription", "local"]
    endpoint_url: str | None = None
    capability: Literal["small", "mid", "frontier"]
    cost: Literal["none", "low", "medium", "high"]
    speed: Literal["slow", "medium", "fast"]
    pool: str = Field(min_length=1)
    weight: int = Field(ge=0)
    enabled: bool
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _local_needs_endpoint(self) -> RoutingModel:
        if self.endpoint == "local" and not self.endpoint_url and self.enabled:
            raise ValueError(f"local model {self.id!r} must carry endpoint_url")
        if self.endpoint == "local" and not self.endpoint_url and not self.disabled_reason:
            raise ValueError(
                f"disabled local model {self.id!r} without endpoint_url must record why"
            )
        if self.endpoint == "subscription" and self.endpoint_url:
            raise ValueError(f"subscription model {self.id!r} must not carry endpoint_url")
        if self.enabled and self.disabled_reason:
            raise ValueError(f"enabled model {self.id!r} must not carry disabled_reason")
        return self


BudgetUnit = Literal["attempts", "tokens_out", "cost_units"]


class RoutingPool(StrictModel):
    window: str = Field(pattern=r"^[0-9]+[hm]$")
    budget_units: BudgetUnit
    soft_limit: int = Field(ge=0)
    # Absent on immutable versions 1 and 2. Only version 3 uses reactive marks.
    default_cooldown_seconds: int = Field(default=3600, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)


class Rotation(StrictModel):
    strategy: str = Field(min_length=1)
    quality_feedback: bool
    quality_window: int = Field(ge=1)


class Reroute(StrictModel):
    reroute_max: int = Field(default=3, ge=0)
    resume_max_wait_seconds: int = Field(default=86400, ge=1)


class RoutingPolicyV1(StrictModel):
    schema_version: str
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    tiers: dict[str, RoutingTier]
    models: list[RoutingModel] = Field(min_length=1)
    pools: dict[str, RoutingPool]
    rotation: Rotation
    reroute: Reroute = Field(default_factory=Reroute)

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _coherent(self) -> RoutingPolicyV1:
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate model id")
        unknown_pools = sorted({m.pool for m in self.models} - set(self.pools))
        if unknown_pools:
            raise ValueError(f"models reference pools the policy does not define: {unknown_pools}")
        if not self.tiers:
            raise ValueError("at least one tier is required")
        return self

    def model(self, model_id: str) -> RoutingModel | None:
        return next((m for m in self.models if m.id == model_id), None)


def parse_policy(document: object) -> PolicyV1:
    return PolicyV1.model_validate(document)


def parse_routing_policy(document: object) -> RoutingPolicyV1:
    return RoutingPolicyV1.model_validate(document)


def window_seconds(window: str) -> int:
    """'5h' or '90m' as seconds. The pattern on RoutingPool.window guarantees the shape."""
    value, unit = int(window[:-1]), window[-1]
    return value * (3600 if unit == "h" else 60)


def policy_document(policy: PolicyV1) -> dict[str, Any]:
    return policy.model_dump(mode="json")
