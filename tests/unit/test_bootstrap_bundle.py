"""The bootstrap bundle's pure half (15): every validation rule of step 2 with its
rejection case, every state mapping, the timestamp normalization, and the content hash
against what `foundry-ledger export --format crucible` actually wrote for its own
synthetic fixture (tests/fixtures_data/bootstrap/foundry_ledger_example.json: four
invented tasks, eleven invented events, made by the real tool)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from crucible.domain.bootstrap import (
    STATE_MAP,
    TASK_FIELDS,
    content_sha256,
    parse_timestamp,
    validate_bundle,
)
from crucible.domain.lifecycle import TaskState
from tests.fixtures import (
    BOOTSTRAP_TASK_FIELDS,
    SYNTHETIC_STATES,
    bootstrap_bundle,
    bootstrap_content_sha256,
    bootstrap_event,
    bootstrap_task,
    synthetic_bundle,
)

PRODUCER_FIXTURE = (
    Path(__file__).parent.parent / "fixtures_data" / "bootstrap" / "foundry_ledger_example.json"
)


def producer_bundle() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(PRODUCER_FIXTURE.read_text(encoding="utf-8"))
    return document


def paths(problems: list[dict[str, str]]) -> list[str]:
    return [p["path"] for p in problems]


# ----- the producer's shape ------------------------------------------------------------


def test_the_producers_own_bundle_validates_and_its_hash_recomputes() -> None:
    raw = producer_bundle()
    bundle, problems = validate_bundle(raw)
    assert problems == [] and bundle is not None
    assert [t.external_id for t in bundle.tasks] == ["EX-0001", "EX-0002", "EX-0003", "EX-0004"]
    assert bundle.content_sha256 == content_sha256(raw["tasks"], raw["events"])
    # The producer writes `id`, not `external_id`, nineteen fields, and `migrated` as
    # the import id or null (c6.md finding 1); the test fields track the producer's.
    assert tuple(raw["tasks"][0]) == TASK_FIELDS == BOOTSTRAP_TASK_FIELDS
    assert raw["source"]["migrated"] is None
    assert [e.seq for e in bundle.events] == list(range(1, 12))
    assert bundle.state_map == {
        "done": {"target": "closed", "count": 2},
        "proposed": {"target": "submitted", "count": 2},
    }


def test_the_synthetic_builder_matches_the_producers_hash_rule() -> None:
    raw = synthetic_bundle()
    assert raw["content_sha256"] == bootstrap_content_sha256(raw["tasks"], raw["events"])
    assert raw["content_sha256"] == content_sha256(raw["tasks"], raw["events"])
    bundle, problems = validate_bundle(raw)
    assert problems == [] and bundle is not None
    assert len(bundle.tasks) == 8 and len(bundle.events) == 15


# ----- state mapping (15 step 2) --------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("done", TaskState.CLOSED),
        ("accepted", TaskState.ACCEPTED),
        ("reported", TaskState.AWAITING_ACCEPTANCE),
        ("running", TaskState.RUNNING),
        ("dispatched", TaskState.RUNNING),
        ("proposed", TaskState.SUBMITTED),
        ("blocked", TaskState.BLOCKED),
        ("abandoned", TaskState.CANCELLED),
    ],
)
def test_each_source_state_maps_as_15_says(source: str, target: TaskState) -> None:
    assert STATE_MAP[source] is target
    bundle, problems = validate_bundle(bootstrap_bundle([bootstrap_task("SYN-0001", source)], []))
    assert problems == [] and bundle is not None
    task = bundle.tasks[0]
    assert task.state is target
    assert task.unsupervised is (source in ("running", "dispatched"))
    assert task.closed is (target in (TaskState.CLOSED, TaskState.CANCELLED))


@pytest.mark.parametrize("source", ["rejected", "missing", "queued", ""])
def test_a_state_without_a_row_in_15s_table_is_a_problem_not_a_guess(source: str) -> None:
    bundle, problems = validate_bundle(bootstrap_bundle([bootstrap_task("SYN-0001", source)], []))
    assert bundle is None
    assert paths(problems) == ["tasks[0].state"]
    assert "no mapping in 15's table" in problems[0]["message"]


def test_the_synthetic_bundle_covers_every_mapped_state() -> None:
    assert {state for _, state in SYNTHETIC_STATES} == set(STATE_MAP)


# ----- timestamps ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01 09:00 CDT", datetime(2026, 9, 1, 14, 0, tzinfo=UTC)),
        ("2026-01-15 09:00 CST", datetime(2026, 1, 15, 15, 0, tzinfo=UTC)),
        ("2026-03-10 08:00 CDT", datetime(2026, 3, 10, 13, 0, tzinfo=UTC)),
        ("2026-09-01T09:00:00-05:00", datetime(2026, 9, 1, 14, 0, tzinfo=UTC)),
        ("2026-09-01T14:00:00Z", datetime(2026, 9, 1, 14, 0, tzinfo=UTC)),
    ],
)
def test_producer_timestamps_normalize_to_utc(value: str, expected: datetime) -> None:
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize(
    "value",
    ["2026-09-01 09:00", "2026-09-01 09:00 PDT", "2026-09-01T09:00:00", "", None, 5],
)
def test_a_timestamp_without_an_offset_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        parse_timestamp(value)


# ----- every rule of step 2, rejected ---------------------------------------------------


def test_every_problem_is_returned_not_only_the_first() -> None:
    raw = synthetic_bundle()
    raw["schema_version"] = "2.0"
    raw["counts"] = {"tasks": 99, "events": 0}
    raw["tasks"][0]["state"] = "missing"
    raw["events"][3]["seq"] = 1
    raw["content_sha256"] = "0" * 64
    bundle, problems = validate_bundle(raw)
    assert bundle is None
    assert set(paths(problems)) == {
        "schema_version",
        "counts.tasks",
        "counts.events",
        "tasks[0].state",
        "events[3].seq",
        "content_sha256",
    }


def test_the_content_hash_is_recomputed_over_tasks_and_events() -> None:
    raw = synthetic_bundle()
    raw["tasks"][2]["title"] = "tampered after export"
    _, problems = validate_bundle(raw)
    assert paths(problems) == ["content_sha256"]
    # The source block is outside the hash, as the producer documents.
    raw = synthetic_bundle()
    raw["source"]["exported_at"] = "2026-09-04 08:00 CDT"
    bundle, problems = validate_bundle(raw)
    assert problems == [] and bundle is not None


def test_counts_must_match_the_arrays() -> None:
    raw = synthetic_bundle()
    raw["counts"]["events"] = len(raw["events"]) - 1
    _, problems = validate_bundle(raw)
    assert paths(problems) == ["counts.events"]


def test_external_ids_must_be_unique_non_empty_and_bounded() -> None:
    tasks = [
        bootstrap_task("SYN-0001", "done"),
        bootstrap_task("SYN-0001", "proposed"),
        bootstrap_task("", "proposed"),
        bootstrap_task("X" * 129, "proposed"),
    ]
    _, problems = validate_bundle(bootstrap_bundle(tasks, []))
    assert paths(problems) == ["tasks[1].id", "tasks[2].id", "tasks[3].id"]
    assert "duplicate of tasks[0].id" in problems[0]["message"]


def test_event_seq_must_strictly_increase() -> None:
    task = bootstrap_task("SYN-0001", "proposed")
    events = [
        bootstrap_event(1, "SYN-0001", "proposed"),
        bootstrap_event(1, "SYN-0001", "note"),
        bootstrap_event(0, "SYN-0001", "note"),
        bootstrap_event(2, "SYN-0001", "note"),
    ]
    _, problems = validate_bundle(bootstrap_bundle([task], events))
    assert paths(problems) == ["events[1].seq", "events[2].seq"]


def test_an_event_must_name_a_task_in_the_bundle() -> None:
    task = bootstrap_task("SYN-0001", "proposed")
    _, problems = validate_bundle(
        bootstrap_bundle([task], [bootstrap_event(1, "SYN-0002", "proposed")])
    )
    assert paths(problems) == ["events[0].task"]


def test_source_migrated_must_be_present_and_is_only_recorded() -> None:
    raw = synthetic_bundle()
    del raw["source"]["migrated"]
    _, problems = validate_bundle(raw)
    assert paths(problems) == ["source.migrated"]
    for value in (None, False, "crucible-import-42"):
        raw = synthetic_bundle()
        raw["source"]["migrated"] = value
        bundle, problems = validate_bundle(raw)
        assert problems == [] and bundle is not None
        assert bundle.source["migrated"] == value
    raw = synthetic_bundle()
    raw["source"]["migrated"] = 42
    _, problems = validate_bundle(raw)
    assert paths(problems) == ["source.migrated"]


def test_unknown_fields_anywhere_are_refused_rather_than_dropped() -> None:
    raw = synthetic_bundle()
    raw["tasks"][0]["priority"] = "high"
    raw["events"][0]["extra"] = 1
    raw["source"]["host"] = "x"
    raw["counts"]["transitions"] = 3
    raw["comment"] = "x"
    # The hash covers tasks and events, so those two edits move it as well.
    raw["content_sha256"] = bootstrap_content_sha256(raw["tasks"], raw["events"])
    _, problems = validate_bundle(raw)
    assert set(paths(problems)) == {
        "tasks[0].priority",
        "events[0].extra",
        "source.host",
        "counts.transitions",
        "comment",
    }


def test_required_task_fields_and_their_types() -> None:
    task = bootstrap_task("SYN-0001", "proposed")
    del task["title"]
    task["contract"] = "not an object"
    task["evidence"] = {"not": "a list"}
    task["objective"] = 5
    task["created"] = "yesterday"
    _, problems = validate_bundle(bootstrap_bundle([task], []))
    assert set(paths(problems)) == {
        "tasks[0].title",
        "tasks[0].contract",
        "tasks[0].evidence",
        "tasks[0].objective",
        "tasks[0].created",
    }


def test_required_event_fields_and_their_types() -> None:
    task = bootstrap_task("SYN-0001", "proposed")
    event = bootstrap_event(1, "SYN-0001", "proposed")
    del event["who"]
    event["detail"] = 5
    event["seq"] = "1"
    event["ts"] = "2026-09-01 09:00"
    _, problems = validate_bundle(bootstrap_bundle([task], [event]))
    assert set(paths(problems)) == {
        "events[0].who",
        "events[0].detail",
        "events[0].seq",
        "events[0].ts",
    }


def test_schema_version_major_must_be_1() -> None:
    for version in ("2.0", "1", "one", 1.0):
        _, problems = validate_bundle(synthetic_bundle(schema_version=version))
        assert "schema_version" in paths(problems), version
    bundle, problems = validate_bundle(synthetic_bundle(schema_version="1.7"))
    assert problems == [] and bundle is not None


def test_a_non_object_bundle_is_one_problem() -> None:
    bundle, problems = validate_bundle([])
    assert bundle is None and paths(problems) == ["$"]


# ----- what the columns carry and what the record keeps (15 step 4) ---------------------


def test_uncarried_fields_are_named_per_task() -> None:
    task = bootstrap_task("SYN-0001", "done", title="t" * 300, project=None, refs={})
    bundle, problems = validate_bundle(bootstrap_bundle([task], []))
    assert problems == [] and bundle is not None
    record = bundle.tasks[0]
    assert len(record.title) == 256 and record.project == ""
    named = {(u["field"], u["carried_as"]) for u in record.uncarried}
    assert ("title", "truncated") in named
    assert ("project", "empty") in named
    # Every field with a value and no column is named; the empty refs is not.
    assert {f for f, how in named if how == "record"} == {
        "scope",
        "objective",
        "contract",
        "model",
        "harness",
        "execution",
    }
    assert "refs" not in {f for f, _ in named}
