"""No table ever holds a token, key, or secret (14): column names are checked."""

from __future__ import annotations

import re

from crucible.adapters.persistence.models import Base
from crucible.domain.events import EventKind

SECRET_NAME = re.compile(r"(secret|password|passwd|private_key|api_key|credential)|(^|_)token$")
# The fenced token is a monotonic counter on the lease row (10, 14), not a credential.
ALLOWED = {"leases.fenced_token"}


def test_no_secret_bearing_column_names() -> None:
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if SECRET_NAME.search(column.name) and f"{table.name}.{column.name}" not in ALLOWED
    ]
    assert offenders == []


def test_migration_event_kinds_match_enum() -> None:
    from crucible.adapters.persistence.migrations.versions import (  # noqa: PLC0415
        _0004_gates_and_acceptance as m,
    )

    # 0004 owns the current CHECK constraint; adding a kind is a new migration (10).
    assert set(m.EVENT_KINDS) == {k.value for k in EventKind}
