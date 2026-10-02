"""Hades #254: every attempt routes with the routing version in force when it is routed.

A correction runs as a new attempt inside the task's history, and its execution carries
the policy snapshot recorded when the task was admitted. When that policy's routing
reference is unpinned, the attempt routes with the newest routing version, so a model
removed or disabled since is never chosen; a pinned reference keeps its version. The
version used is recorded on the attempt, and the policy snapshot stays as recorded.

The supervisor routes the correction against the in-memory store the hades #360 tests
use (the unit tier has no Postgres), with a routing repository holding two versions.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from crucible.application.routing import current_routing_version, load_attempt_routing
from crucible.application.supervisor import _Pending
from crucible.domain.entities import Attempt, Execution, RoutingPolicyRecord
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState
from tests.fixtures import FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    _attach,
    _correction,
    _correction_attempt,
    _ready_for_merge,
    _routing,
    _Store,
    _supervisor,
)


class _RoutingVersions:
    """The routing policy repository over every stored version of every routing policy."""

    def __init__(self, records: Sequence[RoutingPolicyRecord]) -> None:
        self.records = list(records)

    def get(self, name: str, version: int) -> RoutingPolicyRecord | None:
        return next(
            (r for r in self.records if (r.name, r.version) == (name, version)),
            None,
        )

    def list_versions(self, name: str) -> list[RoutingPolicyRecord]:
        return sorted((r for r in self.records if r.name == name), key=lambda r: r.version)


def _model(model_id: str, *, enabled: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = copy.deepcopy(_routing().document["models"][0])
    entry["id"] = model_id
    entry["enabled"] = enabled
    return entry


def _version(version: int, models: list[dict[str, Any]], **fields: Any) -> RoutingPolicyRecord:
    document = copy.deepcopy(_routing().document)
    document["version"] = version
    document["models"] = models
    return RoutingPolicyRecord(
        name="default-routing",
        version=version,
        document=document,
        created_at=NOW + timedelta(minutes=version),
        **fields,
    )


def _store(newest: RoutingPolicyRecord, *, pinned: bool | None = False) -> _Store:
    """A task ready for merge whose policy names default-routing version 3, and a newer
    routing version published after the task was admitted."""
    store = _ready_for_merge()
    ref = store.policies.policy.document["routing"]["policy"]
    assert ref == {"name": "default-routing", "version": 3}
    if pinned is not None:
        ref["pinned"] = pinned
    store.routing_policies = _RoutingVersions([_routing(), newest])  # type: ignore[assignment]
    return store


def _route_correction(store: _Store, tmp_path: Path) -> tuple[Execution, Attempt]:
    clock = FakeClock(NOW)
    _attach(store, _correction(), clock)
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    execution, attempt = _correction_attempt(store)
    assert attempt.state is AttemptState.PENDING
    task = store.tasks.get(attempt.task_id)
    stored = store.contracts.get(attempt.task_id, execution.contract_version)
    assert task is not None and stored is not None
    routed = supervisor._route_pending(_Pending(attempt, execution, task, stored.document))
    assert routed is not None, "the correction was not routed"
    return _correction_attempt(store)


def _routed_payload(store: _Store, attempt: Attempt) -> dict[str, Any]:
    return next(
        e.payload
        for e in store.events.rows
        if e.kind == EventKind.ATTEMPT_ROUTED.value and e.attempt_id == attempt.id
    )


def test_a_correction_routes_with_the_newest_unpinned_routing_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-test", enabled=False), _model("gpt-new")]))

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.state is AttemptState.PREPARING
    assert attempt.selected_model == "gpt-new"
    assert execution.model == "gpt-new"
    assert attempt.routing_version == 4
    assert _routed_payload(store, attempt)["routing_policy"] == {
        "name": "default-routing",
        "version": 4,
    }
    # The snapshot stays as recorded, for reproducibility.
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


def test_an_explicit_unpinned_reference_follows_the_newest_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]), pinned=None)
    store.policies.policy.document["routing"]["policy"]["pinned"] = False

    _execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model == "gpt-new"
    assert attempt.routing_version == 4


def test_a_model_removed_in_the_newest_version_is_not_selected_for_a_correction(
    tmp_path: Path,
) -> None:
    store = _store(_version(4, [_model("gpt-new")]))

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model != "gpt-test"
    assert attempt.selected_model == "gpt-new"
    assert all(c["model"] != "gpt-test" for c in attempt.ordered_candidates)
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


def test_a_pinned_reference_keeps_its_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]), pinned=True)

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model == "gpt-test"
    assert attempt.routing_version == 3
    assert _routed_payload(store, attempt)["routing_policy"]["version"] == 3
    assert execution.policy_snapshot["routing"]["policy"] == {
        "name": "default-routing",
        "version": 3,
        "pinned": True,
    }


def test_a_retired_version_is_not_the_current_one() -> None:
    store = _store(_version(4, [_model("gpt-new")], retired_at=NOW))
    policy = store.policies.policy.document

    assert current_routing_version(store.uow(), policy) == 3
    routing = load_attempt_routing(store.uow(), policy)
    assert routing is not None and routing.version == 3


def test_a_routed_attempt_keeps_the_version_it_was_routed_with() -> None:
    """The launch, the pool reservation and the exit read the version on the attempt,
    not one published after it was routed."""
    store = _store(_version(4, [_model("gpt-new")]))
    policy = store.policies.policy.document

    routing = load_attempt_routing(store.uow(), policy, 3)
    assert routing is not None and routing.version == 3
    assert routing.model("gpt-test") is not None
    current = load_attempt_routing(store.uow(), policy)
    assert current is not None and current.version == 4
