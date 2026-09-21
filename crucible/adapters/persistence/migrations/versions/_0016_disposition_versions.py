"""C7d append-only review disposition versions.

Revision ID: 0016_disposition_versions
Revises: 0015_admin_ui
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0015_admin_ui import (
    EVENT_KINDS as C7A_EVENT_KINDS,
)
from crucible.adapters.persistence.migrations.versions._0015_admin_ui import previous_event_kinds

revision = "0016_disposition_versions"
down_revision = "0015_admin_ui"
branch_labels = None
depends_on = None

EVENT_KINDS = ("disposition_invalidated",)
EVENT_ARCHIVE = "events_c7d_archive"
DISPOSITION_ARCHIVE = "review_dispositions_c7d_archive"


def _event_kinds() -> list[str]:
    return [*previous_event_kinds(), *C7A_EVENT_KINDS, *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
    connection = op.get_bind()
    event_archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{EVENT_ARCHIVE}"}
    ).scalar()
    if event_archive:
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
        op.execute(f"INSERT INTO events SELECT * FROM {EVENT_ARCHIVE}")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")

    op.add_column(
        "review_dispositions",
        sa.Column("comment_body_sha256", sa.String(64), nullable=True),
    )
    op.execute(
        "ALTER TABLE review_dispositions DISABLE TRIGGER trg_review_dispositions_append_only"
    )
    op.execute(
        "UPDATE review_dispositions AS d SET comment_body_sha256 = COALESCE("
        "(SELECT c.body_sha256 FROM review_comments AS c WHERE c.id = d.review_comment_id), "
        f"'{('0' * 64)}')"
    )
    op.execute("ALTER TABLE review_dispositions ENABLE TRIGGER trg_review_dispositions_append_only")
    op.alter_column("review_dispositions", "comment_body_sha256", nullable=False)
    op.drop_constraint(
        "review_dispositions_review_comment_id_key",
        "review_dispositions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_review_dispositions_comment_body",
        "review_dispositions",
        ["review_comment_id", "comment_body_sha256"],
    )

    disposition_archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{DISPOSITION_ARCHIVE}"}
    ).scalar()
    if disposition_archive:
        op.execute(
            "ALTER TABLE review_dispositions DISABLE TRIGGER trg_review_dispositions_append_only"
        )
        op.execute(
            f"INSERT INTO review_dispositions "
            "(id, review_comment_id, principal_id, disposition, reasoning, created_at, "
            "comment_body_sha256) SELECT id, review_comment_id, principal_id, disposition, "
            f"reasoning, created_at, comment_body_sha256 FROM {DISPOSITION_ARCHIVE}"
        )
        op.execute(
            "ALTER TABLE review_dispositions ENABLE TRIGGER trg_review_dispositions_append_only"
        )
        op.execute(f"DROP TABLE {DISPOSITION_ARCHIVE}")


def downgrade() -> None:
    gone = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds([*previous_event_kinds(), *C7A_EVENT_KINDS])

    op.execute(f"CREATE TABLE IF NOT EXISTS {DISPOSITION_ARCHIVE} (LIKE review_dispositions)")
    op.execute(
        "ALTER TABLE review_dispositions DISABLE TRIGGER trg_review_dispositions_append_only"
    )
    op.execute(
        f"INSERT INTO {DISPOSITION_ARCHIVE} "
        "SELECT id, review_comment_id, principal_id, disposition, reasoning, created_at, "
        "comment_body_sha256 FROM (SELECT d.*, ROW_NUMBER() OVER ("
        "PARTITION BY review_comment_id ORDER BY created_at DESC, id DESC) AS version_number "
        "FROM review_dispositions AS d) AS versions WHERE version_number > 1"
    )
    op.execute(
        "DELETE FROM review_dispositions WHERE id IN (SELECT id FROM ("
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY review_comment_id "
        "ORDER BY created_at DESC, id DESC) AS version_number FROM review_dispositions"
        ") AS versions WHERE version_number > 1)"
    )
    op.execute("ALTER TABLE review_dispositions ENABLE TRIGGER trg_review_dispositions_append_only")
    op.drop_constraint("uq_review_dispositions_comment_body", "review_dispositions", type_="unique")
    op.create_unique_constraint(
        "review_dispositions_review_comment_id_key",
        "review_dispositions",
        ["review_comment_id"],
    )
    op.drop_column("review_dispositions", "comment_body_sha256")
