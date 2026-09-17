"""The publication and post-PR gate evaluators (11, 23)."""

from __future__ import annotations

from crucible.domain.gates import (
    POST_PR_GATES,
    PUBLICATION_GATES,
    DeliveryInput,
    GateName,
    GateResult,
    configured,
    evaluate_delivery,
)

HEAD = "a" * 40


def di(**kw: object) -> DeliveryInput:
    base: dict[str, object] = {"policy": {}, "accepted_head": HEAD}
    base.update(kw)
    return DeliveryInput(**base)  # type: ignore[arg-type]


def run(gate: str, **kw: object) -> tuple[GateResult, str]:
    outcome = evaluate_delivery([gate], di(**kw))[gate]
    return outcome.result, outcome.detail


def test_branch_pushed_at_head_is_pending_before_the_push_and_fails_on_a_mismatch() -> None:
    assert run(GateName.BRANCH_PUSHED_AT_HEAD)[0] is GateResult.PENDING
    assert run(GateName.BRANCH_PUSHED_AT_HEAD, branch_pushed_sha="b" * 40)[0] is GateResult.FAIL
    assert run(GateName.BRANCH_PUSHED_AT_HEAD, branch_pushed_sha=HEAD)[0] is GateResult.PASS


def test_pr_exists_head_matches_binds_the_pull_request_to_the_accepted_head() -> None:
    assert run(GateName.PR_EXISTS_HEAD_MATCHES)[0] is GateResult.PENDING
    result, detail = run(GateName.PR_EXISTS_HEAD_MATCHES, pr_number=7, pr_head_sha="b" * 40)
    assert result is GateResult.FAIL and "not the accepted head" in detail
    assert run(GateName.PR_EXISTS_HEAD_MATCHES, pr_number=7, pr_head_sha=HEAD)[0] is GateResult.PASS


def test_external_review_rounds_counts_completed_cycles() -> None:
    assert run(GateName.EXTERNAL_REVIEW_ROUNDS, completed_rounds=0)[0] is GateResult.PENDING
    assert run(GateName.EXTERNAL_REVIEW_ROUNDS, completed_rounds=1)[0] is GateResult.PASS
    assert (
        run(GateName.EXTERNAL_REVIEW_ROUNDS, completed_rounds=1, required_rounds=2)[0]
        is GateResult.PENDING
    )


def test_zero_required_rounds_skips_the_gate() -> None:
    """05b: `required_rounds: 0` makes the external review gates skipped."""
    result, detail = run(GateName.EXTERNAL_REVIEW_ROUNDS, required_rounds=0)
    assert result is GateResult.SKIPPED and "no external review round" in detail


def test_require_review_on_final_sha_holds_the_gate_pending_with_its_reason() -> None:
    result, detail = run(
        GateName.EXTERNAL_REVIEW_ROUNDS,
        completed_rounds=1,
        final_sha=(False, "the only accepted signal is a reaction"),
    )
    assert result is GateResult.PENDING and "reaction" in detail


def test_dispositions_are_complete_when_there_is_nothing_to_disposition() -> None:
    assert run(GateName.FEEDBACK_DISPOSITIONS_COMPLETE)[0] is GateResult.PASS
    assert (
        run(GateName.FEEDBACK_DISPOSITIONS_COMPLETE, comment_count=2, undispositioned=("c1",))[0]
        is GateResult.PENDING
    )
    assert run(GateName.FEEDBACK_DISPOSITIONS_COMPLETE, comment_count=2)[0] is GateResult.PASS


def test_ci_green_for_head_mirrors_the_certification_state() -> None:
    for state, expected in (
        ("green", GateResult.PASS),
        ("failed", GateResult.FAIL),
        ("skipped", GateResult.SKIPPED),
        ("pending", GateResult.PENDING),
        ("", GateResult.PENDING),
    ):
        assert run(GateName.CI_GREEN_FOR_HEAD, certification_state=state)[0] is expected


def test_an_unknown_gate_is_an_error_rather_than_a_silent_pass() -> None:
    outcome = evaluate_delivery(["not_a_gate"], di())["not_a_gate"]
    assert outcome.result is GateResult.ERROR


def test_the_policy_names_the_set_and_no_policy_means_the_whole_set() -> None:
    assert set(configured({}, "publication", PUBLICATION_GATES)) == set(PUBLICATION_GATES)
    assert set(configured({}, "post_pr", POST_PR_GATES)) == set(POST_PR_GATES)
    assert configured({"gates": {"post_pr": []}}, "post_pr", POST_PR_GATES) == []
    assert configured({"gates": {"post_pr": ["ci_green_for_head"]}}, "post_pr", POST_PR_GATES) == [
        "ci_green_for_head"
    ]
