"""Record on each attempt the routing policy name and version it was launched under.

Revision ID: 0036_attempt_routing_version
Revises: 0035_credential_renewer

Hades #359. Existing attempts are backfilled from their execution's policy snapshot,
which is the routing they were launched under. Attempt rows are fenced to the
supervisor, so the trigger is off for the backfill alone, as 0011 does.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_attempt_routing_version"
down_revision = "0035_credential_renewer"
branch_labels = None
depends_on = None

BACKFILL = (
    "UPDATE attempts a SET "
    "routing_policy_name = x.policy_snapshot->'routing'->'policy'->>'name', "
    "routing_policy_version = (x.policy_snapshot->'routing'->'policy'->>'version')::int "
    "FROM executions x WHERE x.id = a.execution_id "
    "AND a.routing_policy_name IS NULL "
    "AND x.policy_snapshot->'routing'->'policy'->>'name' IS NOT NULL "
    "AND x.policy_snapshot->'routing'->'policy'->>'version' ~ '^[0-9]+$'"
)


def upgrade() -> None:
    op.add_column("attempts", sa.Column("routing_policy_name", sa.String(128), nullable=True))
    op.add_column("attempts", sa.Column("routing_policy_version", sa.Integer(), nullable=True))
    op.execute("ALTER TABLE attempts DISABLE TRIGGER trg_attempts_fenced")
    op.execute(BACKFILL)
    op.execute("ALTER TABLE attempts ENABLE TRIGGER trg_attempts_fenced")


def downgrade() -> None:
    op.drop_column("attempts", "routing_policy_version")
    op.drop_column("attempts", "routing_policy_name")
