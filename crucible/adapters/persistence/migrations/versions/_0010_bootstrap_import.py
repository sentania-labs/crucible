"""C6: the bootstrap ledger handoff (15). The `bootstrap_imports` table (14), the
`attempts.unsupervised` flag for the synthetic attempt an imported running task gets,
and the event kinds the import and the commit write.

Revision ID: 0010_bootstrap_import
Revises: 0009_administration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0009_administration import (
    _event_kinds as c5b_event_kinds,
)

revision = "0010_bootstrap_import"
down_revision = "0009_administration"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
C6_EVENT_KINDS = (
    "bootstrap_import_verified",
    "bootstrap_import_committed",
    "bootstrap_task_imported",
    "bootstrap_event_imported",
    "bootstrap_handoff",
)
EVENT_ARCHIVE = "events_c6_archive"


def _event_kinds() -> list[str]:
    return [*c5b_event_kinds(), *C6_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{EVENT_ARCHIVE}"}
        ).scalar()
    )


def upgrade() -> None:
    op.create_table(
        "bootstrap_imports",
        sa.Column("id", ID, primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("imported_by", sa.String(160), nullable=False),
        sa.Column("verified_at", TZ, nullable=False),
        sa.Column("committed_at", TZ, nullable=True),
        sa.Column("committed_by", sa.String(160), nullable=True),
        sa.CheckConstraint(
            "state IN ('verified', 'authoritative')", name="ck_bootstrap_imports_state"
        ),
    )
    op.create_index("ix_bootstrap_imports_content", "bootstrap_imports", ["content_sha256"])
    # ADR 0006: there is never more than one writable ledger, so at most one import
    # holds authority. The database says so, not only the service.
    op.create_index(
        "uq_bootstrap_imports_authoritative",
        "bootstrap_imports",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'authoritative'"),
    )
    # The synthetic attempt of an imported running task (15): no worker behind it, so the
    # supervisor's scans leave it alone. The flag is what makes that explicit.
    op.add_column(
        "attempts",
        sa.Column("unsupervised", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{k}'" for k in _event_kinds())
    op.execute(
        f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed})) NOT VALID"
    )
    op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_kind")
    connection = op.get_bind()
    if _archive_exists(connection):
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute(
            f"INSERT INTO events (seq, ts, kind, task_id, execution_id, attempt_id, "
            f"principal, verified, payload) SELECT seq, ts, kind, task_id, execution_id, "
            f"attempt_id, principal, verified, payload FROM {EVENT_ARCHIVE}"
        )
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute(
            "SELECT setval('events_seq_seq', GREATEST("
            "(SELECT COALESCE(MAX(seq), 1) FROM events), 1))"
        )
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")


def downgrade() -> None:
    # The audit log is never deleted to make a constraint fit (c4.md decision 36).
    gone = ", ".join(f"'{k}'" for k in C6_EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C6_EVENT_KINDS)
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")
    # The synthetic execution and attempt rows stay (their events reference them, and
    # events are append-only); below this revision they are ordinary rows a supervisor
    # cannot observe, which the older code logs per tick and carries on from. The task
    # rows stay as well, and the imported events move to the archive above rather than
    # being lost: the next upgrade puts them back with their seq values.
    op.drop_column("attempts", "unsupervised")
    op.drop_index("uq_bootstrap_imports_authoritative", table_name="bootstrap_imports")
    op.drop_index("ix_bootstrap_imports_content", table_name="bootstrap_imports")
    op.drop_table("bootstrap_imports")
