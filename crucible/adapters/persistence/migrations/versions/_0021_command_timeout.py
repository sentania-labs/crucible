"""crucible#128: the event an edit of the per-command timeout writes.

`limits.command_timeout_ms` lives in the policy document, so no table changes; a save
from the admin surface writes a new policy version and one `command_timeout_updated`
audit event. Existing policy versions are left as they are: one without the field takes
the default bounds (60 minutes, operator decision of 2026-09-25).

Revision ID: 0021_command_timeout
Revises: 0020_provider_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0020_provider_settings import (
    _event_kinds as _previous_event_kinds,
)

revision = "0021_command_timeout"
down_revision = "0020_provider_settings"
branch_labels = None
depends_on = None

EVENT_KINDS = ("command_timeout_updated",)
EVENT_ARCHIVE = "events_128_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
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
    # The audit trail of an edit outlives a rollback, as 0020 keeps its own kinds.
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
