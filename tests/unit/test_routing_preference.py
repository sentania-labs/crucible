"""ADR 0028: Hermes first for trivial and standard work, frontier for complex, and a
demotion that judges a failure rate and lets a model recover."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.application.admin.routing_models import tier_plain_words
from crucible.application.admin.routing_preference import parse_pool_order
from crucible.application.routing import count_blocking_failures, quality_state, select_model
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import AttemptMetrics, PoolExhaustion
from crucible.domain.lifecycle import AttemptState
from tests.unit.test_class_routing import _uow

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
GATEWAY = "http://gateway.lab.test:4000/v1"
MID_FALLBACKS = {"claude-sonnet-5", "gemini-3.8-flash-high"}


def _entry(model_id: str, harness: str, capability: str, pool: str) -> dict[str, Any]:
    local = harness == "hermes"
    return {
        "id": model_id,
        "harness": harness,
        "endpoint": "local" if local else "subscription",
        "endpoint_url": GATEWAY if local else None,
        "capability": capability,
        "cost": "none" if local else "high",
        "speed": "fast",
        "pool": pool,
        "weight": 1,
        "enabled": True,
    }


def _pool() -> dict[str, Any]:
    return {"window": "5h", "budget_units": "attempts", "soft_limit": 0}


def _document(**overrides: Any) -> dict[str, Any]:
    """The seeded tiers (0008) with Hermes, Claude Code and AGY all enabled."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "default-routing",
        "version": 9,
        "tiers": {
            "trivial": {"allowed_capability": ["small", "mid"], "prefer": ["small"]},
            "standard": {"allowed_capability": ["mid", "small"], "prefer": ["mid"]},
            "complex": {"allowed_capability": ["frontier", "mid"], "prefer": ["frontier"]},
        },
        "models": [
            _entry("claude-haiku-4-5", "claude_code", "small", "anthropic-sub"),
            _entry("claude-sonnet-5", "claude_code", "mid", "anthropic-sub"),
            _entry("claude-fable-5-1", "claude_code", "frontier", "anthropic-sub"),
            _entry("gemini-3.8-flash-low", "agy", "small", "google-sub"),
            _entry("gemini-3.8-flash-high", "agy", "mid", "google-sub"),
            _entry("gemini-3.1-pro-high", "agy", "frontier", "google-sub"),
            _entry("coder", "hermes", "mid", "lab-local"),
        ],
        "pools": {"anthropic-sub": _pool(), "google-sub": _pool(), "lab-local": _pool()},
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }
    document.update(overrides)
    return document


def _routing(**overrides: Any) -> RoutingPolicyV1:
    return RoutingPolicyV1.model_validate(_document(**overrides))


def _metric(
    attempt_id: str, model: str, *, at: datetime, failed: bool = False, gated: bool = True
) -> AttemptMetrics:
    return AttemptMetrics(
        attempt_id=attempt_id,
        task_id="task-1",
        model=model,
        harness="hermes",
        endpoint_kind="local",
        pool="lab-local",
        gates_passed=0 if failed or not gated else 3,
        gates_failed=1 if failed else 0,
        created_at=at,
    )


def _history(model: str, outcomes: str, *, last: datetime) -> list[AttemptMetrics]:
    """One attempt per character, oldest first, a minute apart and ending at `last`:
    `f` failed a blocking gate, `p` passed, `-` never reached the gates."""
    count = len(outcomes)
    return [
        _metric(
            f"{model}-{index}",
            model,
            at=last - timedelta(minutes=count - 1 - index),
            failed=kind == "f",
            gated=kind != "-",
        )
        for index, kind in enumerate(outcomes)
    ]


def _pick(
    routing: RoutingPolicyV1, tier: str, rows: list[AttemptMetrics] | None = None, **kw: Any
) -> str | None:
    result = select_model(
        _uow(rows, marks=kw.pop("marks", None), attempts=kw.pop("attempts", None)),
        routing,
        contract={"required_verification": [{"command": "python3 -m unittest tests.test_x"}]},
        tier=tier,
        project="p",
        provider="fake",
        now=kw.pop("now", NOW),
        **kw,
    )
    return result.selected.id if result.selected else None


@pytest.mark.parametrize("tier", ["trivial", "standard"])
def test_hermes_is_the_default_doer_for_trivial_and_standard(tier: str) -> None:
    assert _pick(_routing(), tier) == "coder"


def test_complex_goes_to_frontier() -> None:
    chosen = _pick(_routing(), "complex")
    assert chosen in {"claude-fable-5-1", "gemini-3.1-pro-high"}


def test_plain_words_says_pool_harness_order_and_busy_behavior() -> None:
    routing = _routing()
    routing.tiers["complex"].prefer_pools = ["anthropic-sub", "google-sub"]

    assert tier_plain_words(routing, "complex") == (
        "complex: Claude Code first, then AGY, then Hermes; a busy first choice waits"
    )


def test_hermes_stays_first_after_other_models_ran_and_it_did_not() -> None:
    """Least-recent rotation used to hand Hermes only a share once the others ran."""
    rows = _history("coder", "pppp", last=NOW - timedelta(minutes=1))
    rows += _history("claude-sonnet-5", "p", last=NOW - timedelta(days=3))
    assert _pick(_routing(), "standard", rows) == "coder"


def test_fallback_when_the_preferred_pool_is_exhausted() -> None:
    mark = PoolExhaustion(
        pool="lab-local",
        exhausted_at=NOW,
        reset_at=NOW + timedelta(minutes=30),
        task_id="t",
        attempt_id="a",
        reason="local endpoint failed (provider_error)",
    )
    chosen = _pick(_routing(), "standard", marks=[mark])
    assert chosen in {"claude-sonnet-5", "gemini-3.8-flash-high"}


def test_fallback_when_hermes_is_disabled_or_has_no_credential() -> None:
    chosen = _pick(_routing(), "trivial", eligible_harnesses={"claude_code", "agy"})
    assert chosen in {"claude-haiku-4-5", "gemini-3.8-flash-low"}


def test_explicit_empty_order_lets_the_capability_preference_decide() -> None:
    document = _document()
    document["tiers"]["standard"]["prefer_pools"] = []
    routing = RoutingPolicyV1.model_validate(document)
    rows = _history("claude-sonnet-5", "p", last=NOW - timedelta(hours=1))
    # Every mid model ties on pool and capability; never-run models go first, by id.
    assert _pick(routing, "standard", rows) == "coder"
    assert routing.preferred_pools("standard") == []


def test_explicit_order_is_followed_in_order() -> None:
    document = _document()
    document["tiers"]["complex"]["prefer_pools"] = ["google-sub", "anthropic-sub"]
    routing = RoutingPolicyV1.model_validate(document)
    assert _pick(routing, "complex") == "gemini-3.1-pro-high"


def test_absent_order_reads_as_the_default() -> None:
    routing = _routing()
    assert routing.preferred_pools("trivial") == ["lab-local"]
    assert routing.preferred_pools("standard") == ["lab-local"]
    assert routing.preferred_pools("complex") == []


def test_a_high_failure_rate_moves_routing_and_one_failure_does_not() -> None:
    routing = _routing()
    last = NOW - timedelta(minutes=5)
    one = _history("coder", "ppppf", last=last)
    assert _pick(routing, "standard", one) == "coder"
    many = _history("coder", "pfpfff", last=last)
    chosen = _pick(routing, "standard", many)
    assert chosen in {"claude-sonnet-5", "gemini-3.8-flash-high"}


def test_a_demoted_model_is_probed_after_the_interval_and_recovers() -> None:
    routing = _routing()
    failing = _history("coder", "pfpfff", last=NOW - timedelta(minutes=59))
    assert _pick(routing, "standard", failing) != "coder"
    # Its last attempt is now an hour old: it gets the probe.
    assert _pick(routing, "standard", failing, now=NOW + timedelta(minutes=1)) == "coder"
    # Passing probes bring the rate under the threshold; it is the default again.
    recovered = failing + _history("coder", "ppp", last=NOW + timedelta(hours=3))
    state = quality_state(routing, recovered[-20:], NOW + timedelta(hours=3))
    assert (state.failures, state.sample, state.demoted) == (4, 9, False)
    assert _pick(routing, "standard", recovered, now=NOW + timedelta(hours=3)) == "coder"


def test_below_the_minimum_sample_nothing_is_demoted() -> None:
    state = quality_state(_routing(), _history("coder", "fff", last=NOW), NOW)
    assert not state.demoted


def test_attempts_that_never_reached_the_gates_are_not_judged() -> None:
    state = quality_state(_routing(), _history("coder", "--f-f-p-", last=NOW), NOW)
    assert (state.sample, state.failures, state.demoted) == (3, 2, False)


def test_quality_feedback_off_never_demotes() -> None:
    routing = _routing(rotation={"strategy": "w", "quality_feedback": False, "quality_window": 20})
    assert not quality_state(routing, _history("coder", "ffffff", last=NOW), NOW).demoted


def test_two_failures_are_the_floor_whatever_the_percentage() -> None:
    routing = _routing(
        rotation={
            "strategy": "w",
            "quality_feedback": True,
            "quality_window": 20,
            "demote_failure_percent": 1,
            "demote_min_sample": 2,
        }
    )
    assert not quality_state(routing, _history("coder", "pf", last=NOW), NOW).demoted
    assert quality_state(routing, _history("coder", "ff", last=NOW), NOW).demoted


def test_candidates_say_why_they_rank_where_they_do() -> None:
    result = select_model(
        _uow(_history("coder", "pfpfff", last=NOW)),
        _routing(),
        tier="standard",
        project="p",
        provider="fake",
        now=NOW,
    )
    coder = next(c for c in result.candidates if c["model"] == "coder")
    assert coder["preferred_pool"] is True
    assert coder["quality"] == {
        "sample": 6,
        "blocking_failures": 4,
        "demoted": True,
        "probe": False,
    }
    assert result.candidates[-1]["model"] == "coder"


def test_a_preferred_pool_the_policy_does_not_define_is_refused() -> None:
    document = _document()
    document["tiers"]["standard"]["prefer_pools"] = ["nowhere"]
    with pytest.raises(ValidationError, match="does not define"):
        RoutingPolicyV1.model_validate(document)


def test_a_pool_listed_twice_is_refused() -> None:
    document = _document()
    document["tiers"]["standard"]["prefer_pools"] = ["lab-local", "lab-local"]
    with pytest.raises(ValidationError, match="twice"):
        RoutingPolicyV1.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [("demote_min_sample", 1), ("demote_failure_percent", 0), ("probe_after_minutes", 0)],
)
def test_rotation_bounds(field: str, value: int) -> None:
    rotation = {"strategy": "w", "quality_feedback": True, "quality_window": 20, field: value}
    with pytest.raises(ValidationError):
        _routing(rotation=rotation)


def test_pool_order_spelling() -> None:
    assert parse_pool_order("lab-local, anthropic-sub") == ["lab-local", "anthropic-sub"]
    assert parse_pool_order("lab-local anthropic-sub") == ["lab-local", "anthropic-sub"]
    assert parse_pool_order("") == []
    assert parse_pool_order(" Default ") is None


def test_a_probe_already_routed_is_not_handed_out_twice() -> None:
    """Two tasks routed in one tick: the first takes the probe, the second falls back,
    because the first's metrics row is written only at its launch."""
    routing = _routing()
    failing = _history("coder", "pfpfff", last=NOW - timedelta(minutes=61))
    assert _pick(routing, "standard", failing) == "coder"
    routed = SimpleNamespace(
        state=AttemptState.PREPARING, selected_model="coder", task_id="task-probe"
    )
    assert _pick(routing, "standard", failing, attempts=[routed]) in MID_FALLBACKS


def test_only_blocking_gate_failures_count() -> None:
    gates = [
        SimpleNamespace(result="fail", blocking=False),  # advisory (ADR 0024)
        SimpleNamespace(result="error", blocking=False),
        SimpleNamespace(result="pass", blocking=True),
        SimpleNamespace(result="fail", blocking=True),
        SimpleNamespace(result="error"),  # a record from before the classification
        SimpleNamespace(result="pending"),
    ]
    assert count_blocking_failures(gates) == 2


@pytest.mark.parametrize(
    "rotation",
    [
        {"quality_window": 20, "probe_after_minutes": 10081},
        {"quality_window": 1001},
        {"quality_window": 4, "demote_min_sample": 5},
    ],
)
def test_rotation_limits(rotation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _routing(rotation={"strategy": "w", "quality_feedback": True, **rotation})


@pytest.mark.parametrize(("window", "sample"), [(1, 2), (3, 3), (4, 4), (20, 5)])
def test_a_version_from_before_adr_0028_still_loads(window: int, sample: int) -> None:
    """A stored version may carry a quality window below five and no minimum sample; it
    is immutable, so its default must fit the window it has."""
    routing = _routing(
        rotation={"strategy": "w", "quality_feedback": True, "quality_window": window}
    )
    assert routing.rotation.demote_min_sample == sample
