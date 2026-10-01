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


def test_every_observed_non_skipped_run_is_required() -> None:
    required, source = resolve_required(
        observed=[check("build"), check("skipped-one", "skipped"), check("build")],
    )
    assert required == ("build",) and source == "observed"


def test_policy_only_narrows_observed_runs() -> None:
    result = certify(
        policy(required_checks=["build", "not-observed"]),
        head_sha=HEAD,
        observed=[check("build"), check("scan", "failure")],
    )
    assert result.state is CertificationState.GREEN
    assert result.required == ("build",)
    assert result.source == "observed"
    assert result.detail == f"1 of 1 jobs succeeded on {HEAD}"


def test_narrowing_to_no_observed_runs_is_pending() -> None:
    result = certify(policy(required_checks=["absent"]), head_sha=HEAD, observed=[check("build")])
    assert result.state is CertificationState.PENDING
    assert not result.required


def test_an_empty_set_is_pending_never_green() -> None:
    result = certify(policy(), head_sha=HEAD, observed=[])
    assert result.state is CertificationState.PENDING
    assert "never green" in result.detail


def test_allow_no_ci_makes_the_gate_skipped_not_passed() -> None:
    result = certify(policy(allow_no_ci=True), head_sha=HEAD, observed=[])
    assert result.state is CertificationState.SKIPPED
    assert "intentionally has no CI" in result.detail


def test_green_needs_every_required_member_successful_on_the_accepted_head() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        observed=[check("build"), check("scan")],
    )
    assert result.state is CertificationState.GREEN
    assert result.required == ("build", "scan")
    assert result.detail == f"2 of 2 jobs succeeded on {HEAD}"


def test_a_required_check_only_on_another_head_leaves_the_set_pending() -> None:
    result = certify(
        policy(required_checks=["build"]),
        head_sha=HEAD,
        observed=[check("build", head=OTHER)],
    )
    assert result.state is CertificationState.PENDING
    assert result.pending == ()


def test_a_failure_conclusion_fails_and_names_the_check() -> None:
    for conclusion in (
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    ):
        result = certify(
            policy(),
            head_sha=HEAD,
            observed=[check("scan"), check("build", conclusion)],
        )
        assert result.state is CertificationState.FAILED, conclusion
        assert result.failures[0].name == "build"
        assert "1 of 2 jobs succeeded" in result.detail


def test_neutral_and_skipped_are_not_green() -> None:
    for conclusion in ("neutral", "skipped"):
        result = certify(
            policy(required_checks=["build"]),
            head_sha=HEAD,
            observed=[check("build", conclusion)],
        )
        assert result.state is CertificationState.PENDING, conclusion
        assert result.pending == (("build",) if conclusion == "neutral" else ())
        assert not result.failures


def test_a_running_check_is_pending_not_failed() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        observed=[check("scan"), check("build", None, status="in_progress")],
    )
    assert result.state is CertificationState.PENDING


def test_a_success_with_the_same_name_cannot_hide_a_failure() -> None:
    result = certify(
        policy(required_checks=["build"]),
        head_sha=HEAD,
        observed=[check("build", "failure"), check("build", "success")],
    )
    assert result.state is CertificationState.FAILED


def test_a_check_suite_is_recorded_but_never_becomes_a_required_check() -> None:
    """An App that opens a suite on every head and runs nothing in it would otherwise
    hold every task in `pending` for ever (observed live on the throwaway repository)."""
    suite = check("suite:claude", None, status="queued", source=CheckSource.CHECK_SUITE)
    required, source = resolve_required(observed=[suite])
    assert required == () and source == "observed"
    result = certify(policy(allow_no_ci=True), head_sha=HEAD, observed=[suite])
    assert result.state is CertificationState.SKIPPED
    # It is still recorded as something observed on the head (23).
    assert result.observed == (suite,)


def test_a_workflow_run_counts_alongside_a_check_run() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        observed=[check("ci", source=CheckSource.WORKFLOW_RUN), check("build")],
    )
    assert result.state is CertificationState.GREEN and result.source == "observed"


def test_the_wait_timeout_comes_from_the_policy_with_the_specification_default() -> None:
    assert (
        wait_timeout_hours({"ci_certification": {"wait_timeout_hours": 3}}, "ci_certification", 6)
        == 3
    )
    assert wait_timeout_hours({}, "ci_certification", 6) == 6
    assert wait_timeout_hours({}, "external_review", 24) == 24


def test_skipped_run_and_suite_do_not_mask_a_same_named_job() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        observed=[
            check("build", "failure"),
            check("build", "skipped"),
            check("build", source=CheckSource.CHECK_SUITE),
        ],
    )
    assert result.state is CertificationState.FAILED
    assert "0 of 1 jobs succeeded" in result.detail


def test_queued_job_keeps_successful_jobs_pending() -> None:
    result = certify(
        policy(),
        head_sha=HEAD,
        observed=[
            check("build"),
            check("scan", None, status="queued"),
        ],
    )
    assert result.state is CertificationState.PENDING
    assert "1 of 2 jobs still running" in result.detail
