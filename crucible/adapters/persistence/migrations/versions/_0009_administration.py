"""C5b: the administrative surface (25). The event kinds every admin mutation writes, and
default-software version 2, which names the routing policy with the verified roster.

Revision ID: 0009_administration
Revises: 0008_harness_adapters
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0008_harness_adapters import (
    _event_kinds as c5a_event_kinds,
)

revision = "0009_administration"
down_revision = "0008_harness_adapters"
branch_labels = None
depends_on = None

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
C5B_EVENT_KINDS = (
    "credential_validated",
    "credential_probed",
    "credential_login_started",
    "credential_login_finished",
    "credential_rotated",
    "credential_removed",
    "credential_retired_shredded",
    "image_promoted",
    "github_checked",
    "admin_refused",
)
EVENT_ARCHIVE = "events_c5b_archive"


def _event_kinds() -> list[str]:
    return [*c5a_event_kinds(), *C5B_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{EVENT_ARCHIVE}"}
        ).scalar()
    )


POLICY_V2_DESCRIPTION = (
    "The default software delivery policy, version 2: version 1 with the routing policy "
    "that names the verified model roster (default-routing version 2, C5). Policies are "
    "immutable once referenced, so this is a new version beside version 1."
)


def _seed_policy_v2(connection: sa.engine.Connection) -> None:
    """default-software version 2 = version 1's document naming default-routing
    version 2 (C5b). Version 1 stays for the tasks that reference it (05b)."""
    existing = connection.execute(
        sa.text("SELECT 1 FROM policies WHERE name = :name AND version = 2"),
        {"name": "default-software"},
    ).scalar_one_or_none()
    if existing is not None:
        return
    row = connection.execute(
        sa.text("SELECT document FROM policies WHERE name = :name AND version = 1"),
        {"name": "default-software"},
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("policy default-software/1 is missing; revision 0001 seeds it")
    document = dict(row)
    document["version"] = 2
    document["description"] = POLICY_V2_DESCRIPTION
    document["routing"] = {"policy": {"name": "default-routing", "version": 2}}
    connection.execute(
        sa.text(
            "INSERT INTO policies (name, version, document, created_at) "
            "VALUES (:name, 2, CAST(:document AS jsonb), now())"
        ),
        {"name": "default-software", "document": json.dumps(document)},
    )


def upgrade() -> None:
    _seed_policy_v2(op.get_bind())
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
    op.execute(
        sa.text("DELETE FROM policies WHERE name = :name AND version = 2").bindparams(
            name="default-software"
        )
    )
    # The audit log is never deleted to make a constraint fit (c4.md decision 36).
    gone = ", ".join(f"'{k}'" for k in C5B_EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C5B_EVENT_KINDS)
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")
