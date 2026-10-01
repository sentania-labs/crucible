"""Register Codex credential renewal event kinds.

Revision ID: 0035_credential_renewer
Revises: 0034_external_review_requested

Extend the event vocabulary and credential observation mount modes.
"""

from __future__ import annotations

from alembic import op

from crucible.adapters.persistence.migrations.versions._0034_external_review_requested import (
    _event_kinds as _previous_event_kinds,
)

revision = "0035_credential_renewer"
down_revision = "0034_external_review_requested"
branch_labels = None
depends_on = None

EVENT_KINDS = ("credential_refreshed", "credential_refresh_failed", "credential_refresh_requested")


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def _replace_mount_modes(*, renewer: bool) -> None:
    modes = "'ro', 'rw-narrow', 'renewer'" if renewer else "'ro', 'rw-narrow'"
    op.execute("ALTER TABLE harnesses DROP CONSTRAINT ck_harnesses_mount_mode")
    op.execute(
        "ALTER TABLE harnesses ADD CONSTRAINT ck_harnesses_mount_mode "
        f"CHECK (mount_mode_observed IS NULL OR mount_mode_observed IN ({modes}))"
    )


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
    _replace_mount_modes(renewer=True)


def downgrade() -> None:
    op.execute(
        "UPDATE harnesses SET mount_mode_observed = NULL WHERE mount_mode_observed = 'renewer'"
    )
    _replace_mount_modes(renewer=False)
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    _replace_event_kinds(_previous_event_kinds())
