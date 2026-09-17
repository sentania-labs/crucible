"""C3 correction: the log resume position keeps which line at its instant it is, and
`collection_failed` joins the event kinds.

Lines can repeat inside one timestamp, and `docker logs --since` is inclusive (S8), so
a resume that matched only the last hash in a batch would swallow every repeat between
the stored line and that match. The position is now (timestamp, occurrence, sha256).

Revision ID: 0006_log_occurrence
Revises: 0005_logs_and_retention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0005_logs_and_retention import (
    _event_kinds as c3_event_kinds,
)

revision = "0006_log_occurrence"
down_revision = "0005_logs_and_retention"
branch_labels = None
depends_on = None

C3_CORRECTION_EVENT_KINDS = ("collection_failed",)


def _event_kinds() -> list[str]:
    return [*c3_event_kinds(), *C3_CORRECTION_EVENT_KINDS]


def upgrade() -> None:
    op.add_column(
        "attempts",
        sa.Column("log_resume_occurrence", sa.Integer, nullable=False, server_default="0"),
    )
    op.alter_column("attempts", "log_resume_occurrence", server_default=None)
    op.add_column(
        "log_chunks", sa.Column("occurrence", sa.Integer, nullable=False, server_default="0")
    )
    op.alter_column("log_chunks", "occurrence", server_default=None)

    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{k}'" for k in _event_kinds())
    op.execute(
        f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed})) NOT VALID"
    )
    op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_kind")


def downgrade() -> None:
    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C3_CORRECTION_EVENT_KINDS)
    # `events` is append-only, so the trigger stands down for exactly this statement.
    gone = ", ".join(f"'{k}'" for k in C3_CORRECTION_EVENT_KINDS)
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")

    op.drop_column("log_chunks", "occurrence")
    op.drop_column("attempts", "log_resume_occurrence")
