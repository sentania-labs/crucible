"""C6b class routing, persistent quota marks, and timed resume.

Revision ID: 0011_class_routing
Revises: 0010_bootstrap_import
"""

from __future__ import annotations

import copy
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0010_bootstrap_import import (
    _event_kinds as c6_event_kinds,
)

revision = "0011_class_routing"
down_revision = "0010_bootstrap_import"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)
C6B_EVENT_KINDS = (
    "pool_exhausted",
    "pool_exhaustion_cleared",
    "attempt_routed",
    "task_rerouted",
    "task_awaiting_quota",
    "task_quota_resumed",
    "quota_wip_committed",
)
EVENT_ARCHIVE = "events_c6b_archive"
POLICY_V3_DESCRIPTION = (
    "The default software delivery policy, version 3: class routing and reactive quota "
    "reroute through default-routing version 3 (C6b). Policies are immutable once "
    "referenced, so this is a new version beside version 1."
)


def _event_kinds() -> list[str]:
    return [*c6_event_kinds(), *C6B_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"public.{EVENT_ARCHIVE}"},
        ).scalar()
    )


def _seed_v3(connection: sa.engine.Connection) -> None:
    routing = connection.execute(
        sa.text("SELECT document FROM routing_policies WHERE name=:name AND version=2"),
        {"name": "default-routing"},
    ).scalar_one()
    routing = copy.deepcopy(routing)
    routing["version"] = 3
    cooldowns = {"anthropic-sub": 18000, "openai-sub": 18000, "google-sub": 3600}
    for name, pool in routing["pools"].items():
        pool["default_cooldown_seconds"] = cooldowns.get(name, 3600)
    routing["reroute"] = {"reroute_max": 3, "resume_max_wait_seconds": 86400}
    connection.execute(
        sa.text(
            "INSERT INTO routing_policies(name, version, document, created_at) "
            "VALUES (:name, 3, CAST(:document AS jsonb), now()) ON CONFLICT DO NOTHING"
        ),
        {"name": "default-routing", "document": json.dumps(routing)},
    )
    policy = connection.execute(
        sa.text("SELECT document FROM policies WHERE name=:name AND version=2"),
        {"name": "default-software"},
    ).scalar_one()
    policy = copy.deepcopy(policy)
    policy["version"] = 3
    policy["description"] = POLICY_V3_DESCRIPTION
    policy["routing"] = {"policy": {"name": "default-routing", "version": 3}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES (:name, 3, CAST(:document AS jsonb), now()) ON CONFLICT DO NOTHING"
        ),
        {"name": "default-software", "document": json.dumps(policy)},
    )


def upgrade() -> None:
    op.add_column("tasks", sa.Column("resume_at", TZ, nullable=True))
    op.add_column("tasks", sa.Column("quota_wait_started_at", TZ, nullable=True))
    op.add_column("attempts", sa.Column("selected_model", sa.String(128), nullable=True))
    op.add_column("attempts", sa.Column("selected_harness", sa.String(32), nullable=True))
    op.add_column("attempts", sa.Column("selected_image", sa.Text(), nullable=True))
    op.add_column("attempts", sa.Column("selected_pool", sa.String(128), nullable=True))
    op.add_column(
        "attempts",
        sa.Column(
            "ordered_candidates", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "attempts",
        sa.Column("resume_from_remote", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "pool_exhaustions",
        sa.Column("pool", sa.String(128), primary_key=True),
        sa.Column("exhausted_at", TZ, nullable=False),
        sa.Column("reset_at", TZ, nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("cleared_at", TZ, nullable=True),
        sa.Column("cleared_by", sa.String(160), nullable=True),
        sa.Column("clear_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_pool_exhaustions_reset_at", "pool_exhaustions", ["reset_at"])
    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in _event_kinds())
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")
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
    _seed_v3(op.get_bind())


def downgrade() -> None:
    gone = ", ".join(f"'{kind}'" for kind in C6B_EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in c6_event_kinds())
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM policies p WHERE p.name='default-software' AND p.version=3 "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.policy_name=p.name AND t.policy_version=p.version)"
        )
    )
    connection.execute(
        sa.text(
            "DELETE FROM routing_policies r WHERE r.name='default-routing' AND r.version=3 "
            "AND NOT EXISTS (SELECT 1 FROM policies p WHERE p.document->'routing'->'policy'->>'name'=r.name "
            "AND (p.document->'routing'->'policy'->>'version')::int=r.version)"
        )
    )
    op.drop_index("ix_pool_exhaustions_reset_at", table_name="pool_exhaustions")
    op.drop_table("pool_exhaustions")
    for column in (
        "resume_from_remote",
        "ordered_candidates",
        "selected_pool",
        "selected_image",
        "selected_harness",
        "selected_model",
    ):
        op.drop_column("attempts", column)
    op.drop_column("tasks", "quota_wait_started_at")
    op.drop_column("tasks", "resume_at")
