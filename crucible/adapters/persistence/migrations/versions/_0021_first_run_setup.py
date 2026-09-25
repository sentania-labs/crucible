"""crucible#119, #120, #121: the events of the first-run setup.

`local_gateway_updated` records a save of the local gateway (its URL, whether a key was
set) or of the models picked from it; `github_app_connected` records the GitHub App
credential the service now owns (ADR 0017). The gateway URL itself is the
`local.gateway` row of `provider_settings` (0020) until a model entry carries it, so no
table changes here.

Revision ID: 0021_first_run_setup
Revises: 0020_provider_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0020_provider_settings import (
    _event_kinds as _previous_event_kinds,
)

revision = "0021_first_run_setup"
down_revision = "0020_provider_settings"
branch_labels = None
depends_on = None

EVENT_KINDS = ("local_gateway_updated", "github_app_connected")
EVENT_ARCHIVE = "events_first_run_archive"


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
    # The audit trail outlives a rollback, as in 0017 and 0020: the events move to an
    # archive table and come back on the next upgrade.
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
