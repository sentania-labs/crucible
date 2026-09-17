"""Required-check resolution and CI certification (23, ADR 0009).

The rule an ordinary "is everything green" check gets wrong is the one tested hardest
here: an empty required set is pending, never green.
"""

from __future__ import annotations

from crucible.domain.certification import (
    CertificationState,
    CheckSource,
    ObservedCheck,
    certify,
    resolve_required,
    wait_timeout_hours,
)

HEAD = "a" * 40
OTHER = "b" * 40


def check(
    name: str,
    conclusion: str | None = "success",
    *,
    head: str = HEAD,
    status: str = "completed",
    source: CheckSource = CheckSource.CHECK_RUN,
) -> ObservedCheck:
    return ObservedCheck(
        name=name, status=status, conclusion=conclusion, head_sha=head, source=source
    )


def policy(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = {"required_checks": [], "allow_no_ci": False}
    section.update(overrides)
    return {"ci_certification": section}


def test_the_policy_list_wins_over_branch_protection() -> None:
    required, source = resolve_required(
        policy(required_checks=["build"]),
        branch_protection=["lint", "test"],
        observed=[check("lint")],
    )
    assert required == ("build",) and source == "policy"


def test_branch_protection_is_next_then_every_observed_run() -> None:
    required, source = resolve_required(
        policy(), branch_protection=["lint", "test", "lint"], observed=[check("build")]
    )
    assert required == ("lint", "test") and source == "branch_protection"
    required, source = resolve_required(
        policy(),
        branch_protection=[],
        observed=[check("build"), check("skipped-one", "skipped"), check("build")],
    )
    assert required == ("build",) and source == "observed"


def test_an_empty_set_is_pending_never_green() -> None:
    result = certify(policy(), head_sha=HEAD, branch_protection=[], observed=[])
    assert result.state is CertificationState.PENDING
    assert "never green" in result.detail


def test_allow_no_ci_makes_the_gate_skipped_not_passed() -> None:
    result = certify(policy(allow_no_ci=True), head_sha=HEAD, branch_protection=[], observed=[])
    assert result.state is CertificationState.SKIPPED
    assert "intentionally has no CI" in result.detail


def test_green_needs_every_required_member_successful_on_the_accepted_head() -> None:
    result = certify(
        policy(required_checks=["build", "scan"]),
        head_sha=HEAD,
        branch_protection=[],
        observed=[check("build"), check("scan")],
    )
    assert result.state is CertificationState.GREEN
    assert result.required == ("build", "scan")


def test_a_required_check_only_on_another_head_leaves_the_set_pending() -> None:
    result = certify(
        policy(required_checks=["build"]),
        head_sha=HEAD,
        branch_protection=[],
        observed=[check("build", head=OTHER)],
    )
    assert result.state is CertificationState.PENDING
    assert result.pending == ("build",)


def test_a_failure_conclusion_fails_and_names_the_check() -> None:
    for conclusion in ("failure", "cancelled", "timed_out", "action_required"):
        result = certify(
            policy(required_checks=["build"]),
            head_sha=HEAD,
            branch_protection=[],
            observed=[check("build", conclusion)],
        )
        assert result.state is CertificationState.FAILED, conclusion
        assert result.failures[0].name == "build"


def test_a_running_check_is_pending_not_failed() -> None:
    result = certify(
        policy(required_checks=["build"]),
        head_sha=HEAD,
        branch_protection=[],
        observed=[check("build", None, status="in_progress")],
    )
    assert result.state is CertificationState.PENDING


def test_a_later_run_of_the_same_name_is_what_counts() -> None:
    """A re-run the operator performed replaces the failure it was asked to replace."""
    result = certify(
        policy(required_checks=["build"]),
        head_sha=HEAD,
        branch_protection=[],
        observed=[check("build", "failure"), check("build", "success")],
    )
    assert result.state is CertificationState.GREEN


def test_a_workflow_run_counts_alongside_a_check_run() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        branch_protection=[],
        observed=[check("ci", source=CheckSource.WORKFLOW_RUN)],
    )
    assert result.state is CertificationState.GREEN and result.source == "observed"


def test_the_wait_timeout_comes_from_the_policy_with_the_specification_default() -> None:
    assert (
        wait_timeout_hours({"ci_certification": {"wait_timeout_hours": 3}}, "ci_certification", 6)
        == 3
    )
    assert wait_timeout_hours({}, "ci_certification", 6) == 6
    assert wait_timeout_hours({}, "external_review", 24) == 24
