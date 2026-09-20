from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from crucible.application.routing import select_model
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import AttemptMetrics, PoolExhaustion

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class _Tasks:
    def search(self, **_kw: Any) -> list[Any]:
        return [SimpleNamespace(id="task-1")]


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


class _Marks:
    def __init__(self, marks: list[PoolExhaustion]) -> None:
        self.marks = {mark.pool: mark for mark in marks}

    def get(self, pool: str) -> PoolExhaustion | None:
        return self.marks.get(pool)


class _Images:
    def list_all(self) -> list[Any]:
        return []


def _uow(
    rows: list[AttemptMetrics] | None = None, marks: list[PoolExhaustion] | None = None
) -> Any:
    return SimpleNamespace(
        tasks=_Tasks(),
        attempt_metrics=_Metrics(rows or []),
        pool_exhaustions=_Marks(marks or []),
        image_promotions=_Images(),
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
