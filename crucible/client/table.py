"""`--table`: a short human view of an envelope. Timestamps in the configured local zone.

The JSON envelope is the contract; this is a convenience for a person at a terminal and
carries nothing the envelope does not.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TextIO
from zoneinfo import ZoneInfo

PREFERRED = (
    "id",
    "name",
    "harness",
    "pool",
    "external_id",
    "state",
    "enabled",
    "role",
    "reason",
    "status",
    "title",
    "summary",
    "updated_at",
    "created_at",
    "acked_at",
    "version",
)


def local_time(value: Any, zone: ZoneInfo) -> str:
    if not isinstance(value, str):
        return str(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(zone).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _rows(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, dict) and isinstance(document.get("items"), list):
        return [item for item in document["items"] if isinstance(item, dict)]
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if isinstance(document, dict):
        return [document]
    return [{"value": document}]


def render(envelope: dict[str, Any], zone: ZoneInfo, out: TextIO, err: TextIO) -> None:
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        print(f"error: {error.get('code')}: {error.get('message')}", file=err)
        if error.get("hint"):
            print(f"hint: {error['hint']}", file=err)
        return
    data = envelope.get("data")
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        data = data["task"]
    rows = _rows(data)
    if not rows:
        print("(none)", file=out)
    else:
        columns = [name for name in PREFERRED if any(name in row for row in rows)]
        if not columns:
            columns = list(rows[0])[:6]
        rendered: list[list[str]] = []
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column, "")
                if column.endswith("_at") or column in ("ts", "created", "updated"):
                    value = local_time(value, zone)
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value, sort_keys=True, separators=(",", ":"))
                values.append(str(value))
            rendered.append(values)
        widths = [
            max(len(column), *(len(row[index]) for row in rendered))
            for index, column in enumerate(columns)
        ]
        print("  ".join(c.upper().ljust(widths[i]) for i, c in enumerate(columns)), file=out)
        for rendered_row in rendered:
            print("  ".join(v.ljust(widths[i]) for i, v in enumerate(rendered_row)), file=out)
    for entry in envelope.get("next") or []:
        print(f"next: {entry['action']}: {' '.join(entry['command'])}", file=out)
    for warning in envelope.get("warnings") or []:
        print(f"warning: {warning}", file=err)
