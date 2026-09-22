from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from crucible.application.routing import select_model
from crucible.application.supervisor import Supervisor
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import AttemptMetrics, ImagePromotion, PoolExhaustion

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class _Metrics:
    def __init__(self, rows: list[AttemptMetrics]) -> None:
        self.rows = rows

    def list_since(self, **kw: Any) -> list[AttemptMetrics]:
        task_ids = kw.get("task_ids")
        model = kw.get("model")
        return [
            row
            for row in self.rows
            if (task_ids is None or row.task_id in task_ids)
            and (model is None or row.model == model)
        ]

    def recent_for_project(self, **kw: Any) -> list[AttemptMetrics]:
        models = set(kw["models"])
        limit = int(kw["limit_per_model"])
        found: list[AttemptMetrics] = []
        for model in models:
            rows = sorted(
                (row for row in self.rows if row.model == model),
                key=lambda row: (row.created_at or NOW, row.attempt_id),
            )
            found.extend(rows[-limit:])
        return found


class _Marks:
    def __init__(self, marks: list[PoolExhaustion]) -> None:
        self.marks = {mark.pool: mark for mark in marks}

    def get(self, pool: str) -> PoolExhaustion | None:
        return self.marks.get(pool)


class _Images:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def list_all(self) -> list[Any]:
        return self.rows


def _uow(
    rows: list[AttemptMetrics] | None = None,
    marks: list[PoolExhaustion] | None = None,
    images: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        attempt_metrics=_Metrics(rows or []),
        pool_exhaustions=_Marks(marks or []),
        image_promotions=_Images(images),
    )


def _routing(models: list[dict[str, Any]]) -> RoutingPolicyV1:
    pools = {
        str(model["pool"]): {
            "window": "1h",
            "budget_units": "attempts",
            "soft_limit": 0,
            "default_cooldown_seconds": 60,
        }
        for model in models
    }
    return RoutingPolicyV1.model_validate(
        {
            "schema_version": "1.0",
            "name": "test-routing",
            "version": 1,
            "tiers": {
                "standard": {
                    "allowed_capability": ["mid", "small"],
                    "prefer": ["mid"],
                }
            },
            "models": models,
            "pools": pools,
            "rotation": {
                "strategy": "weighted-least-recent",
                "quality_feedback": True,
                "quality_window": 20,
            },
        }
    )


def _model(
    model_id: str,
    *,
    harness: str = "codex",
    capability: str = "mid",
    pool: str | None = None,
    weight: int = 1,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "harness": harness,
        "endpoint": "subscription",
        "capability": capability,
        "cost": "low",
        "speed": "fast",
        "pool": pool or f"pool-{model_id}",
        "weight": weight,
        "enabled": True,
    }


def _metric(
    attempt_id: str,
    model: str,
    *,
    at: datetime,
    gates_failed: int = 0,
) -> AttemptMetrics:
    return AttemptMetrics(
        attempt_id=attempt_id,
        task_id="task-1",
        model=model,
        harness="codex",
        endpoint_kind="subscription",
        pool=f"pool-{model}",
        gates_failed=gates_failed,
        created_at=at,
    )


def test_selection_tie_is_deterministic_by_model_id() -> None:
    routing = _routing([_model("z-model"), _model("a-model")])
    result = select_model(_uow(), routing, tier="standard", project="p", provider="fake", now=NOW)
    assert result.selected is not None and result.selected.id == "a-model"
    assert [item["model"] for item in result.candidates] == ["a-model", "z-model"]


def test_selection_is_weighted_least_used() -> None:
    routing = _routing([_model("heavy", weight=2), _model("light")])
    rows = [
        _metric("a1", "heavy", at=NOW - timedelta(minutes=2)),
        _metric("a2", "light", at=NOW - timedelta(minutes=1)),
    ]
    result = select_model(
        _uow(rows), routing, tier="standard", project="p", provider="fake", now=NOW
    )
    assert result.selected is not None and result.selected.id == "heavy"


def test_weighted_least_recent_uses_age_times_weight_for_equal_history() -> None:
    routing = _routing([_model("heavy", weight=2), _model("light", weight=1)])
    launched = NOW - timedelta(minutes=5)
    rows = [
        _metric("a1", "heavy", at=launched),
        _metric("a2", "light", at=launched),
    ]
    result = select_model(
        _uow(rows), routing, tier="standard", project="p", provider="fake", now=NOW
    )
    assert result.selected is not None and result.selected.id == "heavy"


def test_weighted_least_recent_tie_ends_at_model_id() -> None:
    routing = _routing([_model("z-model"), _model("a-model")])
    launched = NOW - timedelta(minutes=5)
    rows = [
        _metric("a1", "z-model", at=launched),
        _metric("a2", "a-model", at=launched),
    ]
    result = select_model(
        _uow(rows), routing, tier="standard", project="p", provider="fake", now=NOW
    )
    assert result.selected is not None and result.selected.id == "a-model"


def test_quality_failure_demotes_one_preference_step() -> None:
    routing = _routing([_model("mid", capability="mid"), _model("small", capability="small")])
    result = select_model(
        _uow([_metric("a1", "mid", at=NOW, gates_failed=1)]),
        routing,
        tier="standard",
        project="p",
        provider="fake",
        now=NOW,
    )
    assert result.selected is not None and result.selected.id == "small"


def test_live_exhaustion_mark_excludes_the_pool_and_records_the_reason() -> None:
    routing = _routing([_model("first"), _model("second")])
    mark = PoolExhaustion(
        pool="pool-first",
        exhausted_at=NOW,
        reset_at=NOW + timedelta(minutes=5),
        task_id="task-1",
        attempt_id="a1",
        reason="quota",
    )
    result = select_model(
        _uow(marks=[mark]), routing, tier="standard", project="p", provider="fake", now=NOW
    )
    assert result.selected is not None and result.selected.id == "second"
    excluded = next(item for item in result.candidates if item["model"] == "first")
    assert excluded["eligible"] is False
    assert excluded["excluded"] == [f"pool exhausted until {mark.reset_at.isoformat()}"]


def test_operator_pin_is_exact_and_does_not_fall_through() -> None:
    routing = _routing([_model("first"), _model("second", harness="agy")])
    result = select_model(
        _uow(),
        routing,
        tier="standard",
        project="p",
        provider="fake",
        now=NOW,
        pinned_model="second",
        pinned_harness="codex",
    )
    assert result.selected is None
    second = next(item for item in result.candidates if item["model"] == "second")
    assert "harness does not match the operator pin" in second["excluded"]


def test_image_allowlist_excludes_one_candidate_and_selects_the_next() -> None:
    routing = _routing([_model("first"), _model("second", harness="agy")])
    images = [
        ImagePromotion(
            digest="sha256:first",
            reference="workers/codex:1",
            harnesses={"codex": "0.156.0"},
            state="default",
            updated_at=NOW,
            updated_by="tests",
        ),
        ImagePromotion(
            digest="sha256:second",
            reference="workers/agy:1",
            harnesses={"agy": "1.2.1"},
            state="default",
            updated_at=NOW,
            updated_by="tests",
        ),
    ]
    result = select_model(
        _uow(images=images),
        routing,
        tier="standard",
        project="p",
        provider="docker",
        now=NOW,
        image_allowlist=["workers/agy:*"],
    )
    assert result.selected is not None and result.selected.id == "second"
    first = next(candidate for candidate in result.candidates if candidate["model"] == "first")
    assert first["excluded"] == ["derived image is outside the policy allowlist"]


def test_image_exclusions_are_ignored_when_deciding_if_usable_models_are_quota_blocked() -> None:
    selection = SimpleNamespace(
        candidates=(
            {
                "model": "image-disallowed",
                "excluded": ["derived image is outside the policy allowlist"],
            },
            {
                "model": "quota-blocked",
                "excluded": [f"pool exhausted until {(NOW + timedelta(minutes=5)).isoformat()}"],
            },
        )
    )
    assert Supervisor._selection_is_quota_blocked(selection) is True


def test_task_event_scan_has_no_one_thousand_event_cap() -> None:
    rows = [SimpleNamespace(seq=index) for index in range(1, 1502)]

    class Events:
        def list_for_task(self, _task_id: str, *, after_seq: int, limit: int) -> list[Any]:
            return [row for row in rows if row.seq > after_seq][:limit]

    found = Supervisor._all_task_events(SimpleNamespace(events=Events()), "task-1")
    assert len(found) == 1501


def test_quota_reset_keeps_any_future_provider_reset() -> None:
    default = NOW + timedelta(minutes=5)
    reset, accepted = Supervisor._bounded_quota_reset(
        NOW, NOW - timedelta(seconds=1), max_seconds=3600, default_seconds=300
    )
    assert reset == default and accepted is None
    candidate = NOW + timedelta(days=2)
    reset, accepted = Supervisor._bounded_quota_reset(
        NOW, candidate, max_seconds=3600, default_seconds=300
    )
    assert reset == candidate and accepted == candidate
