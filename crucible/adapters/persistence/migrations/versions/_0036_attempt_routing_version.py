"""Record the routing policy version each attempt was routed with.

Revision ID: 0036_attempt_routing_version
Revises: 0035_credential_renewer

hades #254: an attempt routes with the routing version in force when it is routed
(an unpinned policy reference follows the newest version), so the version used is
kept on the attempt. Attempts already routed when this runs are backfilled with the
version their execution's policy snapshot references, so an attempt in flight at deploy
keeps the routing it launched with for its exit, reservation and harness count instead
of floating to the newest version. Pending attempts, and attempts whose snapshot has no
routing reference, keep NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_attempt_routing_version"
down_revision = "0035_credential_renewer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("routing_version", sa.Integer(), nullable=True))
    op.execute("ALTER TABLE attempts DISABLE TRIGGER trg_attempts_fenced")
    op.execute(
        "UPDATE attempts a SET routing_version="
        "(x.policy_snapshot->'routing'->'policy'->>'version')::integer "
        "FROM executions x WHERE x.id=a.execution_id "
        "AND a.selected_model IS NOT NULL AND a.state <> 'pending' "
        "AND a.routing_version IS NULL "
        "AND jsonb_typeof(x.policy_snapshot->'routing'->'policy'->'version') = 'number'"
    )
    op.execute("ALTER TABLE attempts ENABLE TRIGGER trg_attempts_fenced")


def downgrade() -> None:
    op.drop_column("attempts", "routing_version")
