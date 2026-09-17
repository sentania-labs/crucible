"""Review cycles, components, and completed rounds (23, ADR 0008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crucible.domain.external_review import (
    Cycle,
    CycleState,
    Signal,
    SignalKind,
    apply_signal,
    completed_rounds,
    component_for,
    configured_components,
    final_sha_satisfied,
    head_at,
    is_accepted,
    required_rounds,
    reviewer_logins,
)

REVIEWER = "chatgpt-codex-connector[bot]"
T0 = datetime(2026, 9, 16, 16, 16, 32, tzinfo=UTC)


def policy(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = {
        "reviewer_logins": [REVIEWER],
        "required_rounds": 1,
        "accepted_signals": ["reaction:+1", "review", "comment"],
        "components": ["code"],
    }
    section.update(overrides)
    return {"external_review": section}


def cycle(components: tuple[str, ...] = ("code",), head: str = "a" * 40) -> Cycle:
    return Cycle(id="01CYCLE", head_sha=head, components=components, opened_at=T0)


def reaction(content: str = "+1", login: str = REVIEWER, at: datetime = T0) -> Signal:
    return Signal(
        kind=SignalKind.REACTION, login=login, github_id="700001", created_at=at, content=content
    )


def review(body: str = "", login: str = REVIEWER, sha: str | None = "a" * 40) -> Signal:
    return Signal(
        kind=SignalKind.REVIEW,
        login=login,
        github_id="500001",
        created_at=T0,
        body=body,
        reviewed_sha=sha,
        has_findings=True,
    )


def test_defaults_come_from_the_policy_and_fall_back_to_05b() -> None:
    assert configured_components(policy()) == ("code",)
    assert configured_components(policy(components=["code", "security"])) == ("code", "security")
    assert configured_components({}) == ("code",)
    assert reviewer_logins({}) == frozenset({REVIEWER})
    assert required_rounds({}) == 1


def test_only_an_allowlisted_login_is_accepted() -> None:
    assert is_accepted(reaction(), policy())
    assert not is_accepted(reaction(login="a-passer-by"), policy())
    assert not is_accepted(review(login="someone"), policy())


def test_a_signal_shape_the_policy_does_not_accept_satisfies_nothing() -> None:
    assert not is_accepted(reaction(content="eyes"), policy())
    assert not is_accepted(reaction(), policy(accepted_signals=["review"]))


def test_a_clean_reaction_completes_every_configured_component_at_once() -> None:
    """23: the provider emits one combined verdict, so one `+1` finishes the cycle."""
    open_cycle = cycle(components=("code", "security"))
    done, completed, just_done = apply_signal(open_cycle, reaction(), at=T0)
    assert set(completed) == {"code", "security"}
    assert just_done and done.state is CycleState.COMPLETED
    assert completed_rounds([done]) == 1


def test_a_review_object_completes_the_component_its_body_names_else_code() -> None:
    assert component_for(review(body="Security review: no findings"), ("code", "security")) == (
        "security",
    )
    assert component_for(review(body="Codex Review"), ("code", "security")) == ("code",)


def test_a_two_component_cycle_stays_open_until_the_second_result() -> None:
    open_cycle = cycle(components=("code", "security"))
    open_cycle, completed, just_done = apply_signal(
        open_cycle, review(body="code review findings"), at=T0
    )
    assert completed == ("code",) and not just_done
    assert open_cycle.outstanding == ("security",)
    assert completed_rounds([open_cycle]) == 0
    open_cycle, completed, just_done = apply_signal(
        open_cycle, review(body="security review clean"), at=T0
    )
    assert completed == ("security",) and just_done
    assert completed_rounds([open_cycle]) == 1


def test_a_second_signal_on_a_completed_cycle_adds_no_round() -> None:
    done, _, _ = apply_signal(cycle(), reaction(), at=T0)
    again, completed, just_done = apply_signal(done, review(), at=T0)
    assert completed == () and not just_done
    assert completed_rounds([again]) == 1


def test_a_reaction_binds_to_the_head_the_pr_carried_at_its_created_at() -> None:
    """S12: a reaction carries no commit id, so the binding is inferred, never a field."""
    heads = [("a" * 40, T0), ("b" * 40, T0 + timedelta(minutes=30))]
    sha, inferred = head_at(heads, T0 + timedelta(minutes=5))
    assert sha == "a" * 40 and inferred
    sha, inferred = head_at(heads, T0 + timedelta(minutes=45))
    assert sha == "b" * 40 and inferred
    sha, inferred = head_at(heads, T0 - timedelta(minutes=5), fallback="c" * 40)
    assert sha == "c" * 40 and inferred


def test_require_review_on_final_sha_cannot_be_met_by_a_reaction() -> None:
    ok, detail = final_sha_satisfied([cycle()], [reaction()], "a" * 40)
    assert not ok and "carries no commit id" in detail


def test_require_review_on_final_sha_is_met_by_a_review_naming_the_head() -> None:
    ok, detail = final_sha_satisfied([cycle()], [review(sha="a" * 40)], "a" * 40)
    assert ok and "a" * 40 in detail
    ok, detail = final_sha_satisfied([cycle()], [review(sha="b" * 40)], "a" * 40)
    assert not ok and "not the accepted head" in detail


@pytest.mark.parametrize("rounds", [0, 2, 3])
def test_required_rounds_is_read_from_the_policy(rounds: int) -> None:
    assert required_rounds(policy(required_rounds=rounds)) == rounds
