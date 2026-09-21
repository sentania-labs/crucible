"""C7a administrative UI event kinds.

Revision ID: 0015_admin_ui
Revises: 0014_enable_hermes_local
"""

from __future__ import annotations

from alembic import op

from crucible.adapters.persistence.migrations.versions._0012_heartbeats import (
    _event_kinds,
)

revision = "0015_admin_ui"
down_revision = "0014_enable_hermes_local"
branch_labels = None
depends_on = None

EVENT_KINDS = [
    "credential_login_code_submitted",
    "credential_login_cancelled",
    "principal_revoked",
    "repository_removed",
]


def previous_event_kinds() -> list[str]:
    return _event_kinds()


def _replace_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_kinds([*previous_event_kinds(), *EVENT_KINDS])


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    _replace_kinds(previous_event_kinds())
