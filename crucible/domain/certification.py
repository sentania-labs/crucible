"""CI certification as a pure decision over observed runs (23, ADR 0009).

The rule that matters most is the one an "is everything green" check gets wrong: an
empty required-check set is **pending**, never green. Before GitHub has created any run,
and on a repository with no CI at all, the task waits and the timeout wakes Foundry. A
repository that intentionally has no CI needs `allow_no_ci`, which makes the gate
`skipped` rather than passed, and that is an operator-recorded policy decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

SUCCESS = "success"
NEUTRAL = "neutral"
SKIPPED = "skipped"
FAILING_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
)
PASSING_CONCLUSIONS: frozenset[str] = frozenset({SUCCESS, NEUTRAL, SKIPPED})


class CertificationState(StrEnum):
    PENDING = "pending"
    GREEN = "green"
    FAILED = "failed"
    SKIPPED = "skipped"


class CheckSource(StrEnum):
    CHECK_RUN = "check_run"
    WORKFLOW_RUN = "workflow_run"


@dataclass(frozen=True, slots=True)
class ObservedCheck:
    """One check run or workflow run on a head, as GitHub reported it."""

    name: str
    status: str
    conclusion: str | None
    head_sha: str
    source: CheckSource = CheckSource.CHECK_RUN
    url: str = ""
    external_id: str = ""
    workflow: str = ""
    job: str = ""

    @property
    def concluded(self) -> bool:
        return self.status == "completed" and self.conclusion is not None

    @property
    def succeeded(self) -> bool:
        return self.concluded and (self.conclusion or "") in PASSING_CONCLUSIONS

    @property
    def failed(self) -> bool:
        return self.concluded and (self.conclusion or "") in FAILING_CONCLUSIONS


@dataclass(frozen=True, slots=True)
class Certification:
    state: CertificationState
    detail: str
    required: tuple[str, ...] = ()
    source: str = ""
    failures: tuple[ObservedCheck, ...] = ()
    pending: tuple[str, ...] = ()
    observed: tuple[ObservedCheck, ...] = field(default=())


def required_checks_from_policy(policy: dict[str, object]) -> tuple[str, ...]:
    section = policy.get("ci_certification")
    raw = section.get("required_checks") if isinstance(section, dict) else None
    if not raw or not isinstance(raw, list):
        return ()
    return tuple(str(name) for name in raw if str(name))


def allow_no_ci(policy: dict[str, object]) -> bool:
    section = policy.get("ci_certification")
    return bool(section.get("allow_no_ci", False)) if isinstance(section, dict) else False


def wait_timeout_hours(policy: dict[str, object], section_name: str, default: int) -> int:
    section = policy.get(section_name)
    if not isinstance(section, dict):
        return default
    return int(section.get("wait_timeout_hours", default))


def resolve_required(
    policy: dict[str, object],
    *,
    branch_protection: Sequence[str],
    observed: Sequence[ObservedCheck],
) -> tuple[tuple[str, ...], str]:
    """23's resolution order: the policy's list, else branch protection or the ruleset,
    else every non-skipped run observed on the head."""
    from_policy = required_checks_from_policy(policy)
    if from_policy:
        return from_policy, "policy"
    protected = tuple(dict.fromkeys(str(name) for name in branch_protection if str(name)))
    if protected:
        return protected, "branch_protection"
    names = tuple(
        dict.fromkeys(
            check.name
            for check in observed
            if not (check.concluded and check.conclusion == SKIPPED)
        )
    )
    return names, "observed"


def certify(
    policy: dict[str, object],
    *,
    head_sha: str,
    branch_protection: Sequence[str],
    observed: Sequence[ObservedCheck],
) -> Certification:
    """Green, failed, pending, or skipped for one head. Never green on an empty set."""
    on_head = tuple(check for check in observed if check.head_sha == head_sha)
    required, source = resolve_required(
        policy, branch_protection=branch_protection, observed=on_head
    )
    if not required:
        if allow_no_ci(policy):
            return Certification(
                CertificationState.SKIPPED,
                "the policy records that this repository intentionally has no CI "
                "(ci_certification.allow_no_ci)",
                source=source,
                observed=on_head,
            )
        return Certification(
            CertificationState.PENDING,
            f"no check run or workflow run has been observed on {head_sha}; an empty "
            "required-check set is pending, never green (23)",
            source=source,
            observed=on_head,
        )
    by_name: dict[str, list[ObservedCheck]] = {}
    for check in on_head:
        by_name.setdefault(check.name, []).append(check)
    failures: list[ObservedCheck] = []
    pending: list[str] = []
    for name in required:
        runs = by_name.get(name, [])
        if not runs:
            pending.append(name)
            continue
        latest = runs[-1]
        if latest.failed:
            failures.append(latest)
        elif not latest.succeeded:
            pending.append(name)
    if failures:
        names = ", ".join(sorted({check.name for check in failures}))
        return Certification(
            CertificationState.FAILED,
            f"required check(s) failed on {head_sha}: {names}",
            required=required,
            source=source,
            failures=tuple(failures),
            pending=tuple(pending),
            observed=on_head,
        )
    if pending:
        return Certification(
            CertificationState.PENDING,
            f"waiting on {len(pending)} required check(s) on {head_sha}: "
            f"{', '.join(sorted(pending))}",
            required=required,
            source=source,
            pending=tuple(pending),
            observed=on_head,
        )
    return Certification(
        CertificationState.GREEN,
        f"every required check concluded successfully on {head_sha} "
        f"({len(required)} check(s), set from {source})",
        required=required,
        source=source,
        observed=on_head,
    )
