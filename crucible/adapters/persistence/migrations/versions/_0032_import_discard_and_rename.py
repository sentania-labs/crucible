"""A verified bootstrap import can be discarded, and a principal renamed (ADR 0029).

Two event kinds: `bootstrap_import_discarded` and `principal_renamed`. The import's
state constraint gains `discarded`. No table changes otherwise: a discard retires the
import's tasks under a disabled principal of their own rather than deleting anything,
because events are append-only (10).

Revision ID: 0032_import_discard_and_rename
Revises: 0031_kubernetes_timeouts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0031_kubernetes_timeouts import (
    _event_kinds as _previous_event_kinds,
)

revision = "0032_import_discard_and_rename"
down_revision = "0031_kubernetes_timeouts"
branch_labels = None
depends_on = None

EVENT_KINDS = ("bootstrap_import_discarded", "principal_renamed")
EVENT_ARCHIVE = "events_0032_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def _replace_import_states(states: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{state}'" for state in states)
    op.execute("ALTER TABLE bootstrap_imports DROP CONSTRAINT ck_bootstrap_imports_state")
    op.execute(
        "ALTER TABLE bootstrap_imports ADD CONSTRAINT ck_bootstrap_imports_state "
        f"CHECK (state IN ({allowed}))"
    )


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
    _replace_import_states(("verified", "authoritative", "discarded"))
    connection = op.get_bind()
    archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{EVENT_ARCHIVE}"}
    ).scalar()
    if archive:
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
        op.execute(f"INSERT INTO events SELECT * FROM {EVENT_ARCHIVE}")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")


def downgrade() -> None:
    # The audit trail of a discard or a rename outlives a rollback, as 0031 keeps its own
    # kinds. A discarded import has no state the older constraint allows, so a rollback
    # with one present is refused rather than rewriting what happened.
    connection = op.get_bind()
    discarded = connection.execute(
        sa.text("SELECT count(*) FROM bootstrap_imports WHERE state = 'discarded'")
    ).scalar()
    if discarded:
        raise RuntimeError(
            f"{discarded} discarded bootstrap import(s) exist; 0031 has no state for them"
        )
    _replace_import_states(("verified", "authoritative"))
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
