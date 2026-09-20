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
TASK_WAIT_ARCHIVE = "task_waits_c6b_archive"
ATTEMPT_ROUTE_ARCHIVE = "attempt_routes_c6b_archive"
POOL_ARCHIVE = "pool_exhaustions_c6b_archive"
POLICY_V3_DESCRIPTION = (
    "The default software delivery policy, version 3: class routing and reactive quota "
    "reroute through default-routing version 3 (C6b). Policies are immutable once "
    "referenced, so this is a new version beside version 1."
)


def _event_kinds() -> list[str]:
    return [*c6_event_kinds(), *C6B_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection, name: str = EVENT_ARCHIVE) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"public.{name}"},
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
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
        op.execute(
            f"INSERT INTO events (seq, ts, kind, task_id, execution_id, attempt_id, "
            f"principal, verified, payload) SELECT e.seq, e.ts, e.kind, e.task_id, "
            f"e.execution_id, e.attempt_id, e.principal, e.verified, e.payload "
            f"FROM {EVENT_ARCHIVE} e WHERE "
            "(e.task_id IS NULL OR EXISTS (SELECT 1 FROM tasks t WHERE t.id=e.task_id)) "
            "AND (e.execution_id IS NULL OR EXISTS "
            "(SELECT 1 FROM executions x WHERE x.id=e.execution_id)) "
            "AND (e.attempt_id IS NULL OR EXISTS "
            "(SELECT 1 FROM attempts a WHERE a.id=e.attempt_id))"
        )
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
        op.execute(
            "SELECT setval('events_seq_seq', GREATEST("
            "(SELECT COALESCE(MAX(seq), 1) FROM events), 1))"
        )
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")
    if _archive_exists(connection, TASK_WAIT_ARCHIVE):
        op.execute(
            f"UPDATE tasks t SET state=a.state, resume_at=a.resume_at, "
            f"quota_wait_started_at=a.quota_wait_started_at FROM {TASK_WAIT_ARCHIVE} a "
            "WHERE t.id=a.task_id"
        )
        op.execute(f"DROP TABLE {TASK_WAIT_ARCHIVE}")
    if _archive_exists(connection, ATTEMPT_ROUTE_ARCHIVE):
        op.execute("ALTER TABLE attempts DISABLE TRIGGER trg_attempts_fenced")
        op.execute(
            f"UPDATE attempts x SET selected_model=a.selected_model, "
            "selected_harness=a.selected_harness, selected_image=a.selected_image, "
            "selected_pool=a.selected_pool, ordered_candidates=a.ordered_candidates, "
            f"resume_from_remote=a.resume_from_remote FROM {ATTEMPT_ROUTE_ARCHIVE} a "
            "WHERE x.id=a.attempt_id"
        )
        op.execute("ALTER TABLE attempts ENABLE TRIGGER trg_attempts_fenced")
        op.execute(f"DROP TABLE {ATTEMPT_ROUTE_ARCHIVE}")
    if _archive_exists(connection, POOL_ARCHIVE):
        op.execute(
            f"INSERT INTO pool_exhaustions "
            f"SELECT a.* FROM {POOL_ARCHIVE} a "
            "JOIN tasks t ON t.id=a.task_id JOIN attempts x ON x.id=a.attempt_id "
            "ON CONFLICT (pool) DO UPDATE SET "
            "exhausted_at=EXCLUDED.exhausted_at, reset_at=EXCLUDED.reset_at, "
            "task_id=EXCLUDED.task_id, attempt_id=EXCLUDED.attempt_id, reason=EXCLUDED.reason, "
            "cleared_at=EXCLUDED.cleared_at, cleared_by=EXCLUDED.cleared_by, "
            "clear_reason=EXCLUDED.clear_reason"
        )
        op.execute(f"DROP TABLE {POOL_ARCHIVE}")
    _seed_v3(op.get_bind())


def downgrade() -> None:
    gone = ", ".join(f"'{kind}'" for kind in C6B_EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in c6_event_kinds())
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")
    connection = op.get_bind()
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {TASK_WAIT_ARCHIVE} AS "
        "SELECT id AS task_id, state, resume_at, quota_wait_started_at FROM tasks WHERE false"
    )
    op.execute(
        f"INSERT INTO {TASK_WAIT_ARCHIVE} "
        "SELECT id, state, resume_at, quota_wait_started_at FROM tasks "
        "WHERE state='awaiting_quota' OR resume_at IS NOT NULL OR quota_wait_started_at IS NOT NULL"
    )
    op.execute("UPDATE tasks SET state='reported' WHERE state='awaiting_quota'")
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {ATTEMPT_ROUTE_ARCHIVE} AS "
        "SELECT id AS attempt_id, selected_model, selected_harness, selected_image, "
        "selected_pool, ordered_candidates, resume_from_remote FROM attempts WHERE false"
    )
    op.execute(
        f"INSERT INTO {ATTEMPT_ROUTE_ARCHIVE} "
        "SELECT id, selected_model, selected_harness, selected_image, selected_pool, "
        "ordered_candidates, resume_from_remote FROM attempts "
        "WHERE selected_model IS NOT NULL OR selected_harness IS NOT NULL "
        "OR selected_image IS NOT NULL OR selected_pool IS NOT NULL "
        "OR ordered_candidates <> '[]'::jsonb OR resume_from_remote"
    )
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {POOL_ARCHIVE} AS SELECT * FROM pool_exhaustions WHERE false"
    )
    op.execute(f"INSERT INTO {POOL_ARCHIVE} SELECT * FROM pool_exhaustions")
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
