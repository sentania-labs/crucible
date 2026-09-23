"""crucible#91: runtime provider settings, and the event an edit of one writes.

`provider_settings` holds one document per setting name. The first is
`kubernetes.egress`: the cluster resolver's and an in-cluster local endpoint's
namespace and pod labels, which the Kubernetes provider turns into selector rules a
CNI that translates service addresses first (Cilium with kube-proxy replacement) still
matches. The settings file seeds it; a saved row wins in every process from then on.

Nothing is seeded: an absent row means the settings file's values.

Revision ID: 0020_provider_settings
Revises: 0019_opus_5_5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0017_lab_local import (
    _event_kinds as _previous_event_kinds,
)

revision = "0020_provider_settings"
down_revision = "0019_opus_5_5"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
EVENT_KINDS = ("kubernetes_egress_updated",)
EVENT_ARCHIVE = "events_91_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.create_table(
        "provider_settings",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(160), nullable=False),
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
    # The audit trail of an edit outlives a rollback: the events move to an archive
    # table and come back on the next upgrade, as 0017 does for its own kinds.
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
    op.drop_table("provider_settings")
