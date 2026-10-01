"""Add CREDENTIAL_REFRESH_REQUESTED event kind (339).

Revision ID: 0036_credential_refresh_requested
Revises: 0035_credential_renewer

Adds the refresh-requested event kind so the API can signal a forced
credential refresh to the supervisor's renewer tick.
"""

from __future__ import annotations

from alembic import op

from crucible.adapters.persistence.migrations.versions._0035_credential_renewer import (
    _event_kinds as _previous_event_kinds,
)

revision = "0036_credential_refresh_requested"
down_revision = "0035_credential_renewer"
branch_labels = None
depends_on = None

NEW_KIND = "credential_refresh_requested"


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    kinds = [*_previous_event_kinds(), NEW_KIND]
    _replace_event_kinds(kinds)


def downgrade() -> None:
    op.execute(f"DELETE FROM events WHERE kind = '{NEW_KIND}'")
    kinds = _previous_event_kinds()
    _replace_event_kinds(kinds)
