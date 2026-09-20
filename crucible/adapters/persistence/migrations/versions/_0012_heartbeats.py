"""C6c worker heartbeats and stall event kinds.

Revision ID: 0012_heartbeats
Revises: 0011_class_routing
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0011_class_routing import (
    _event_kinds as c6b_event_kinds,
)

revision = "0012_heartbeats"
down_revision = "0011_class_routing"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)
EVENT_KINDS = ("worker_quiet", "worker_stalled")
EVENT_ARCHIVE = "events_c6c_archive"


def _event_kinds() -> list[str]:
    return [*c6b_event_kinds(), *EVENT_KINDS]


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("signal", sa.String(32), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
    )
    op.create_index("ix_heartbeats_attempt_ts", "heartbeats", ["attempt_id", "ts"])
    op.execute(
        "CREATE TRIGGER trg_heartbeats_fenced BEFORE INSERT OR UPDATE ON heartbeats "
        "FOR EACH ROW EXECUTE FUNCTION crucible_check_fenced_token();"
    )
    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in _event_kinds())
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")
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
    gone = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in c6b_event_kinds())
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")
    op.execute("DROP TRIGGER IF EXISTS trg_heartbeats_fenced ON heartbeats")
    op.drop_index("ix_heartbeats_attempt_ts", table_name="heartbeats")
    op.drop_table("heartbeats")
