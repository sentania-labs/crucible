"""C3: the log stream, retention actions, and the attempt columns the Docker provider
fills in.

`log_chunks` is the stream of 10, fenced to the supervisor like every other table it
writes. `retention_actions` records each deletion and the policy version that
authorized it (16), with one row per (kind, subject) so a second retention pass is a
no-op. The attempt gains its drain marker, its log resume position, and the moment its
workspace was cleaned.

Revision ID: 0005_logs_and_retention
Revises: 0004_gates_and_acceptance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0004_gates_and_acceptance import (
    EVENT_KINDS as C2_EVENT_KINDS,
)

revision = "0005_logs_and_retention"
down_revision = "0004_gates_and_acceptance"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)

NEW_FENCED_TABLES = ("log_chunks", "retention_actions")

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
C3_EVENT_KINDS = (
    "attempt_logs_drained",
    "attempt_cleaned_up",
    "workspace_prepared",
    "checkout_lease_taken",
    "checkout_lease_denied",
    "checkout_lease_released",
    "image_resolved",
    "collector_rejected_file",
    "verification_completed",
    "retention_applied",
)


def _event_kinds() -> list[str]:
    """The C2 list plus C3's. Reading the applied revision keeps the two in step
    without editing it."""
    return [*C2_EVENT_KINDS, *C3_EVENT_KINDS]


def upgrade() -> None:
    for column, kind in (
        ("logs_drained_at", TZ),
        ("log_resume_ts", TZ),
        ("log_resume_sha256", sa.String(64)),
        ("cleaned_up_at", TZ),
    ):
        op.add_column("attempts", sa.Column(column, kind, nullable=True))

    op.create_table(
        "log_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("stream", sa.String(8), nullable=False),
        sa.Column("offset_start", sa.BigInteger, nullable=False),
        sa.Column("offset_end", sa.BigInteger, nullable=False),
        sa.Column("ts", TZ, nullable=False),
        # The resume position of 10: the sha256 of the last line in this chunk.
        sa.Column("line_sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("gzipped", sa.Boolean, nullable=False),
        sa.CheckConstraint("stream IN ('stdout', 'stderr')", name="ck_log_chunks_stream"),
        sa.UniqueConstraint("attempt_id", "offset_start", name="uq_log_chunks_attempt_offset"),
    )
    op.create_index("ix_log_chunks_attempt", "log_chunks", ["attempt_id", "id"])

    op.create_table(
        "retention_actions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer, nullable=False),
        sa.Column("acted_at", TZ, nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.UniqueConstraint("kind", "subject", name="uq_retention_actions_kind_subject"),
    )
    op.create_index("ix_retention_actions_kind", "retention_actions", ["kind", "acted_at"])

    for table in NEW_FENCED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_fenced BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION crucible_check_fenced_token();"
        )

    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{k}'" for k in _event_kinds())
    op.execute(
        f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed})) NOT VALID"
    )
    op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_kind")


def downgrade() -> None:
    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C3_EVENT_KINDS)
    # `events` is append-only, so the trigger stands down for exactly this statement.
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"DELETE FROM events WHERE kind IN ({', '.join(repr(k) for k in C3_EVENT_KINDS)})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")

    for table in NEW_FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_fenced ON {table};")
    op.drop_table("retention_actions")
    op.drop_table("log_chunks")
    for column in ("cleaned_up_at", "log_resume_sha256", "log_resume_ts", "logs_drained_at"):
        op.drop_column("attempts", column)
