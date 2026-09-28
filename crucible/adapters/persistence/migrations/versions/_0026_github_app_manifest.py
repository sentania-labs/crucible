"""crucible#168: the one-click GitHub App (the manifest flow, ADR 0017 as amended).

`github_manifest_states` holds one row per start of the flow: the sha256 of the `state`
value GitHub carries back (never the value), who started it, the App name and account it
asked for, the external URL the redirect uses, when it expires and when it was used. A
state is good once. Two event kinds: `github_app_manifest_started` records a start, and
`github_external_url_updated` a save of the `github.external_url` setting (a row of
`provider_settings`, 0020), which overrides the URL taken from the operator's browser.

Revision ID: 0026_github_app_manifest
Revises: 0025_private_checkout
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0022_first_run_setup import (
    _event_kinds as _previous_event_kinds,
)

revision = "0026_github_app_manifest"
down_revision = "0025_private_checkout"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
EVENT_KINDS = ("github_app_manifest_started", "github_external_url_updated")
EVENT_ARCHIVE = "events_168_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.create_table(
        "github_manifest_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("principal", sa.String(160), nullable=False),
        sa.Column("app_name", sa.String(64), nullable=False),
        sa.Column("organization", sa.String(64), nullable=True),
        sa.Column("external_url", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("consumed_at", TZ, nullable=True),
    )
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
    # The audit trail outlives a rollback, as in 0022: the events move to an archive
    # table and come back on the next upgrade. A pending start is not worth keeping.
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
    op.drop_table("github_manifest_states")
