"""Register Codex credential renewal event kinds.

Revision ID: 0034_credential_renewer
Revises: 0033_ui_sessions

Event kinds are stored as strings, so this migration intentionally has no DDL. The
revision makes the domain vocabulary change visible in schema history.
"""

from __future__ import annotations

from alembic import op

from crucible.adapters.persistence.migrations.versions._0032_import_discard_and_rename import (
    _event_kinds as _previous_event_kinds,
)

revision = "0034_credential_renewer"
down_revision = "0033_ui_sessions"
branch_labels = None
depends_on = None

EVENT_KINDS = ("credential_refreshed", "credential_refresh_failed")


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    _replace_event_kinds(_previous_event_kinds())
