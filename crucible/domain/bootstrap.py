"""The bootstrap ledger handoff (15), the pure half: what a `BootstrapExportV1` bundle
must look like, how each Foundry state maps onto a Crucible task state, how the
producer's local timestamps normalize to UTC, and the content hash both sides compute.

Everything here is checked against what `foundry-ledger export --format crucible`
actually writes, not against the prose of 15 alone; where the two differ the producer
wins and `docs/implementation-notes/c6.md` records the difference. Validation collects
every problem rather than stopping at the first, because 15 step 2 promises the full
problem list and a bundle that fails stores nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

from crucible.domain.lifecycle import TaskState
from crucible.domain.time import parse_rfc3339

SCHEMA_VERSION: Final = "1.0"
SUPPORTED_MAJOR: Final = "1"

# The top level of the bundle, in the order the producer writes it.
BUNDLE_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "source",
    "tasks",
    "events",
    "counts",
    "content_sha256",
)
SOURCE_KEYS: Final[tuple[str, ...]] = ("tool", "db_sha256", "exported_at", "migrated")

# A task record as the producer emits it: foundry-ledger's TASK_FIELDS, in its order.
# The producer's `id` is the orchestrator's stable id, which Crucible stores as
# `external_id` (03); 15's example writes the key as `external_id`, the producer
# writes `id`, and the producer is what is validated here (c6.md).
TASK_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "title",
    "parent",
    "project",
    "repository",
    "scope",
    "objective",
    "contract",
    "model",
    "harness",
    "execution",
    "state",
    "created",
    "updated",
    "refs",
    "last_report",
    "evidence",
    "blockers",
    "decisions_pending",
)
TASK_REQUIRED: Final[frozenset[str]] = frozenset({"id", "title", "state", "created", "updated"})
TASK_MAPPING_FIELDS: Final[frozenset[str]] = frozenset({"contract", "refs"})
TASK_LIST_FIELDS: Final[frozenset[str]] = frozenset({"evidence", "blockers", "decisions_pending"})
TASK_SCALAR_FIELDS: Final[frozenset[str]] = frozenset(TASK_FIELDS) - (
    TASK_MAPPING_FIELDS | TASK_LIST_FIELDS
)
# The task fields that land in a column of `tasks`. Every other field is carried only
# in the task's `bootstrap_task_imported` event payload, and the verification report
# says so per task (15 step 4: "a diff of any field that could not be carried").
TASK_COLUMN_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "title", "project", "repository", "state", "created", "updated"}
)
EVENT_FIELDS: Final[tuple[str, ...]] = ("seq", "ts", "task", "event", "who", "detail")

EXTERNAL_ID_MAX: Final = 128
TITLE_MAX: Final = 256
PROJECT_MAX: Final = 128

# 15 step 2, verbatim. A state with no row here is a validation problem, never a guess:
# the producer's lifecycle also has `rejected` and `missing`, which 15 does not map
# (c6.md records the finding).
STATE_MAP: Final[dict[str, TaskState]] = {
    "done": TaskState.CLOSED,
    "accepted": TaskState.ACCEPTED,
    "reported": TaskState.AWAITING_ACCEPTANCE,
    "running": TaskState.RUNNING,
    "dispatched": TaskState.RUNNING,
    "proposed": TaskState.SUBMITTED,
    "blocked": TaskState.BLOCKED,
    "abandoned": TaskState.CANCELLED,
}
# The source states that become a Crucible `running` task with a synthetic `bootstrap`
# execution and an attempt marked unsupervised (15 step 2).
UNSUPERVISED_SOURCE_STATES: Final[frozenset[str]] = frozenset({"running", "dispatched"})
# The producer's closed states (foundry-ledger CLOSED_STATES): what "live" excludes.
SOURCE_CLOSED_STATES: Final[frozenset[str]] = frozenset({"done", "abandoned"})
BOOTSTRAP_PROVIDER: Final = "bootstrap"

# The producer's timestamp: local America/Chicago wall time with the zone abbreviation,
# "YYYY-MM-DD HH:MM CDT|CST" (foundry-ledger model.TIMESTAMP_FORMAT). The abbreviation
# fixes the offset, so the conversion is exact and needs no zone database.
LOCAL_TIMESTAMP: Final = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}) (?P<zone>C[DS]T)$"
)
LOCAL_OFFSETS: Final[dict[str, timedelta]] = {
    "CDT": timedelta(hours=-5),
    "CST": timedelta(hours=-6),
}

Problem = dict[str, str]


def problem(path: str, message: str) -> Problem:
    return {"path": path, "message": message}


def content_sha256(tasks: list[Any], events: list[Any]) -> str:
    """The producer's `bundle_content_hash`: sha256 of the canonical JSON (sorted keys,
    no whitespace, UTF-8, non-ASCII unescaped) of {"tasks": [...], "events": [...]}."""
    canonical = json.dumps(
        {"tasks": tasks, "events": events},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_timestamp(value: object) -> datetime:
    """Normalize a producer timestamp to UTC. Accepts the producer's local form and an
    RFC 3339 string with an explicit offset; anything else raises ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    matched = LOCAL_TIMESTAMP.match(value)
    if matched is not None:
        naive = datetime.strptime(f"{matched['date']} {matched['time']}", "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(LOCAL_OFFSETS[matched["zone"]])
        )
        return naive.astimezone(UTC)
    try:
        return parse_rfc3339(value)
    except ValueError:
        raise ValueError(
            "timestamp is neither 'YYYY-MM-DD HH:MM CDT|CST' nor RFC 3339 with an offset"
        ) from None


def check_major_version(value: object) -> str | None:
    if not isinstance(value, str):
        return "schema_version must be a string"
    major, _, minor = value.partition(".")
    if major != SUPPORTED_MAJOR or not minor.isdigit():
        return f"schema_version {value!r} is not a supported 1.x version"
    return None


@dataclass(slots=True)
class TaskRecord:
    """One validated task of the bundle, with its Crucible mapping decided."""

    external_id: str
    source_state: str
    state: TaskState
    title: str
    project: str
    repository: str | None
    created_at: datetime
    updated_at: datetime
    record: dict[str, Any]
    # Fields the columns could not carry as they are, and why (15 step 4).
    uncarried: list[dict[str, str]] = field(default_factory=list)

    @property
    def unsupervised(self) -> bool:
        return self.source_state in UNSUPERVISED_SOURCE_STATES

    @property
    def closed(self) -> bool:
        return self.state in (TaskState.CLOSED, TaskState.CANCELLED)


@dataclass(slots=True)
class EventRecord:
    seq: int
    ts: datetime
    ts_original: str
    external_id: str
    event: str
    who: str
    detail: str | None
    record: dict[str, Any]


@dataclass(slots=True)
class ValidatedBundle:
    schema_version: str
    source: dict[str, Any]
    tasks: list[TaskRecord]
    events: list[EventRecord]
    content_sha256: str

    @property
    def state_map(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in self.tasks:
            entry = out.setdefault(task.source_state, {"target": task.state.value, "count": 0})
            entry["count"] += 1
        return dict(sorted(out.items()))


def _check_scalar(record: dict[str, Any], key: str, where: str, problems: list[Problem]) -> None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        problems.append(problem(f"{where}.{key}", "must be a string or null"))


def _validate_task(index: int, raw: object, problems: list[Problem]) -> TaskRecord | None:
    where = f"tasks[{index}]"
    if not isinstance(raw, dict):
        problems.append(problem(where, "must be an object"))
        return None
    before = len(problems)
    unknown = [k for k in raw if k not in TASK_FIELDS]
    for key in unknown:
        problems.append(
            problem(f"{where}.{key}", "unknown field; the import carries no field it cannot name")
        )
    for key in sorted(TASK_REQUIRED - set(raw)):
        problems.append(problem(f"{where}.{key}", "required"))
    for key in TASK_SCALAR_FIELDS:
        if key in raw:
            _check_scalar(raw, key, where, problems)
    for key in TASK_MAPPING_FIELDS:
        if key in raw and raw[key] is not None and not isinstance(raw[key], dict):
            problems.append(problem(f"{where}.{key}", "must be an object or null"))
    for key in TASK_LIST_FIELDS:
        if key in raw and raw[key] is not None and not isinstance(raw[key], list):
            problems.append(problem(f"{where}.{key}", "must be a list or null"))
    external_id = raw.get("id")
    if isinstance(external_id, str):
        if not external_id.strip():
            problems.append(problem(f"{where}.id", "must not be empty"))
        elif len(external_id) > EXTERNAL_ID_MAX:
            problems.append(problem(f"{where}.id", f"longer than {EXTERNAL_ID_MAX} characters"))
    state = raw.get("state")
    target: TaskState | None = None
    if isinstance(state, str):
        target = STATE_MAP.get(state)
        if target is None:
            problems.append(
                problem(
                    f"{where}.state",
                    f"state {state!r} has no mapping in 15's table; the import does not guess",
                )
            )
    stamps: dict[str, datetime] = {}
    for key in ("created", "updated"):
        if key in raw:
            try:
                stamps[key] = parse_timestamp(raw[key])
            except ValueError as exc:
                problems.append(problem(f"{where}.{key}", str(exc)))
    if len(problems) != before or target is None or not isinstance(external_id, str):
        return None

    uncarried: list[dict[str, str]] = []
    title = raw.get("title")
    if title is None:
        title = ""
        uncarried.append({"field": "title", "carried_as": "empty", "detail": "null title"})
    elif len(title) > TITLE_MAX:
        uncarried.append(
            {
                "field": "title",
                "carried_as": "truncated",
                "detail": f"{len(title)} characters; the column holds {TITLE_MAX}",
            }
        )
        title = title[:TITLE_MAX]
    project = raw.get("project")
    if project is None:
        project = ""
        uncarried.append({"field": "project", "carried_as": "empty", "detail": "null project"})
    elif len(project) > PROJECT_MAX:
        uncarried.append(
            {
                "field": "project",
                "carried_as": "truncated",
                "detail": f"{len(project)} characters; the column holds {PROJECT_MAX}",
            }
        )
        project = project[:PROJECT_MAX]
    for key in TASK_FIELDS:
        if key in raw and key not in TASK_COLUMN_FIELDS and raw[key] not in (None, {}, []):
            uncarried.append(
                {
                    "field": key,
                    "carried_as": "record",
                    "detail": "no task column; kept in the bootstrap_task_imported payload",
                }
            )
    return TaskRecord(
        external_id=external_id,
        source_state=state if isinstance(state, str) else "",
        state=target,
        title=title,
        project=project,
        repository=raw.get("repository"),
        created_at=stamps["created"],
        updated_at=stamps["updated"],
        record=dict(raw),
        uncarried=uncarried,
    )


def _validate_event(
    index: int, raw: object, known_ids: set[str], problems: list[Problem]
) -> EventRecord | None:
    where = f"events[{index}]"
    if not isinstance(raw, dict):
        problems.append(problem(where, "must be an object"))
        return None
    before = len(problems)
    for key in raw:
        if key not in EVENT_FIELDS:
            problems.append(
                problem(
                    f"{where}.{key}", "unknown field; the import carries no field it cannot name"
                )
            )
    for key in EVENT_FIELDS:
        if key not in raw:
            problems.append(problem(f"{where}.{key}", "required"))
    seq = raw.get("seq")
    if "seq" in raw and (not isinstance(seq, int) or isinstance(seq, bool)):
        problems.append(problem(f"{where}.seq", "must be an integer"))
    for key in ("task", "event", "who"):
        value = raw.get(key)
        if key in raw and (not isinstance(value, str) or not value):
            problems.append(problem(f"{where}.{key}", "must be a non-empty string"))
    detail = raw.get("detail")
    if detail is not None and not isinstance(detail, str):
        problems.append(problem(f"{where}.detail", "must be a string or null"))
    task = raw.get("task")
    if isinstance(task, str) and task and task not in known_ids:
        problems.append(problem(f"{where}.task", f"names task {task!r}, which the bundle lacks"))
    ts: datetime | None = None
    if "ts" in raw:
        try:
            ts = parse_timestamp(raw["ts"])
        except ValueError as exc:
            problems.append(problem(f"{where}.ts", str(exc)))
    if len(problems) != before or ts is None or not isinstance(seq, int):
        return None
    return EventRecord(
        seq=seq,
        ts=ts,
        ts_original=str(raw["ts"]),
        external_id=str(task),
        event=str(raw["event"]),
        who=str(raw["who"]),
        detail=detail,
        record=dict(raw),
    )


def _validate_source(raw: object, problems: list[Problem]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        problems.append(problem("source", "must be an object"))
        return {}
    for key in raw:
        if key not in SOURCE_KEYS:
            problems.append(problem(f"source.{key}", "unknown field"))
    for key in SOURCE_KEYS:
        if key not in raw:
            problems.append(problem(f"source.{key}", "required"))
    for key in ("tool", "db_sha256", "exported_at"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key]):
            problems.append(problem(f"source.{key}", "must be a non-empty string"))
    # `migrated` is informational (15): the producer writes the Crucible import id it was
    # frozen with, or null; it is recorded and never decides anything.
    migrated = raw.get("migrated")
    if "migrated" in raw and migrated is not None and not isinstance(migrated, str | bool):
        problems.append(problem("source.migrated", "must be a string, a boolean, or null"))
    return dict(raw)


def validate_bundle(raw: object) -> tuple[ValidatedBundle | None, list[Problem]]:
    """15 step 2 over one parsed JSON document. Returns the validated bundle and an
    empty list, or None and every problem found."""
    problems: list[Problem] = []
    if not isinstance(raw, dict):
        return None, [problem("$", "the bundle must be a JSON object")]
    for key in raw:
        if key not in BUNDLE_KEYS:
            problems.append(problem(key, "unknown field"))
    for key in BUNDLE_KEYS:
        if key not in raw:
            problems.append(problem(key, "required"))
    version_problem = check_major_version(raw.get("schema_version", ""))
    if version_problem is not None:
        problems.append(problem("schema_version", version_problem))
    source = _validate_source(raw.get("source"), problems)

    raw_tasks = raw.get("tasks")
    raw_events = raw.get("events")
    if not isinstance(raw_tasks, list):
        problems.append(problem("tasks", "must be a list"))
        raw_tasks = []
    if not isinstance(raw_events, list):
        problems.append(problem("events", "must be a list"))
        raw_events = []

    tasks: list[TaskRecord] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw_tasks):
        record = _validate_task(index, item, problems)
        if record is None:
            continue
        if record.external_id in seen:
            problems.append(
                problem(
                    f"tasks[{index}].id",
                    f"duplicate of tasks[{seen[record.external_id]}].id {record.external_id!r}",
                )
            )
            continue
        seen[record.external_id] = index
        tasks.append(record)
    known_ids = {
        str(t["id"]) for t in raw_tasks if isinstance(t, dict) and isinstance(t.get("id"), str)
    }

    events: list[EventRecord] = []
    last_seq: int | None = None
    for index, item in enumerate(raw_events):
        event = _validate_event(index, item, known_ids, problems)
        if event is None:
            continue
        if last_seq is not None and event.seq <= last_seq:
            problems.append(
                problem(
                    f"events[{index}].seq",
                    f"{event.seq} does not increase on the previous event's {last_seq}",
                )
            )
        last_seq = event.seq
        events.append(event)

    counts = raw.get("counts")
    if not isinstance(counts, dict):
        problems.append(problem("counts", "must be an object"))
    else:
        for key, actual in (("tasks", len(raw_tasks)), ("events", len(raw_events))):
            declared = counts.get(key)
            if not isinstance(declared, int) or isinstance(declared, bool):
                problems.append(problem(f"counts.{key}", "must be an integer"))
            elif declared != actual:
                problems.append(
                    problem(f"counts.{key}", f"declares {declared}; the array holds {actual}")
                )
        for key in counts:
            if key not in ("tasks", "events"):
                problems.append(problem(f"counts.{key}", "unknown field"))

    declared_hash = raw.get("content_sha256")
    if isinstance(raw_tasks, list) and isinstance(raw_events, list):
        actual_hash = content_sha256(raw_tasks, raw_events)
        if declared_hash != actual_hash:
            problems.append(
                problem(
                    "content_sha256",
                    "does not match the hash recomputed over the bundle's tasks and events",
                )
            )
    if problems:
        return None, problems
    return (
        ValidatedBundle(
            schema_version=str(raw["schema_version"]),
            source=source,
            tasks=tasks,
            events=events,
            content_sha256=str(declared_hash),
        ),
        [],
    )
