"""C5: harness adapters live. The per-harness runtime record (enable flag with its
reason, session compatibility, what runs observed about the credential) and the image
promotion table (13), plus the event kinds the registry and the credential sync write.

Seeded defaults follow S1b: Claude Code's dedicated session is verified and enabled;
Codex and AGY stay disabled until their Crucible-side refresh has been observed and the
operator's own session confirmed afterwards; the script harness has no credential and
is enabled for the e2e tier.

Revision ID: 0008_harness_adapters
Revises: 0007_github_delivery
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0007_github_delivery import (
    _event_kinds as c4_event_kinds,
)

revision = "0008_harness_adapters"
down_revision = "0007_github_delivery"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
C5_EVENT_KINDS = (
    "harness_refused",
    "harness_launch_deferred",
    "harness_enabled",
    "harness_disabled",
    "credential_synced",
)

C5_TABLES = ("image_promotions", "harnesses")
EVENT_ARCHIVE = "events_c5_archive"

UNVERIFIED_REASON = (
    "unverified: the dedicated session's Crucible-side token refresh has not yet been "
    "observed, so daily-session compatibility is not established (S1b steps 5 and 6)"
)
SEED = (
    ("claude_code", True, "session_compatibility verified (S1b); enabled for workers", "verified"),
    ("codex", False, UNVERIFIED_REASON, "unverified"),
    ("agy", False, UNVERIFIED_REASON, "unverified"),
    ("script-harness", True, "the e2e tier's harness (18): no model, no credential", "verified"),
)


def _event_kinds() -> list[str]:
    return [*c4_event_kinds(), *C5_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{EVENT_ARCHIVE}"}
        ).scalar()
    )


def upgrade() -> None:
    op.create_table(
        "harnesses",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("session_compatibility", sa.String(16), nullable=False),
        sa.Column("mount_mode_observed", sa.String(16), nullable=True),
        sa.Column("refresh_requires_rw", sa.Boolean, nullable=True),
        sa.Column("last_launch_at", TZ, nullable=True),
        sa.Column("last_launch_outcome", sa.String(48), nullable=True),
        sa.Column("last_auth_failure_at", TZ, nullable=True),
        sa.Column("last_validated_at", TZ, nullable=True),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(160), nullable=False),
        sa.CheckConstraint(
            "session_compatibility IN ('unverified', 'verified', 'failed')",
            name="ck_harnesses_session_compatibility",
        ),
        sa.CheckConstraint(
            "mount_mode_observed IS NULL OR mount_mode_observed IN ('ro', 'rw-narrow')",
            name="ck_harnesses_mount_mode",
        ),
    )
    op.create_table(
        "image_promotions",
        sa.Column("digest", sa.String(160), primary_key=True),
        sa.Column("reference", sa.Text, nullable=False),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("harness_version", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(160), nullable=False),
        sa.CheckConstraint(
            "state IN ('candidate', 'default', 'retained')", name="ck_image_promotions_state"
        ),
    )
    for name, enabled, reason, compatibility in SEED:
        op.execute(
            sa.text(
                "INSERT INTO harnesses (name, enabled, reason, session_compatibility, "
                "updated_at, updated_by) VALUES (:name, :enabled, :reason, :compatibility, "
                "now(), 'migration')"
            ).bindparams(name=name, enabled=enabled, reason=reason, compatibility=compatibility)
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
    for table in C5_TABLES:
        op.drop_table(table)
    # The audit log is never deleted to make a constraint fit (c4.md decision 36): the C5
    # rows move to an archive this migration leaves behind, and its upgrade moves them
    # back. The append-only trigger stands down for exactly the move.
    gone = ", ".join(f"'{k}'" for k in C5_EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C5_EVENT_KINDS)
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")
