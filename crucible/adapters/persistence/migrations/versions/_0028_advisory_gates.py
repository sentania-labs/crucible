"""ADR 0024: each pre-PR gate result says whether it stops the task.

`gate_results.blocking` records the class the gate had when it was evaluated: a failed
blocking gate stops the task, a failed advisory one is carried in front of the internal
reviewer. Every row written before this revision was blocking, because every gate was,
so the column defaults to true. `gate_results.findings` holds advisory findings that do
not change the result, such as a worker's report contradicting Crucible's re-run; older
rows have none.

No policy version is rewritten: a version without `gates.advisory` takes the default
classification when it is read (crucible.domain.gates.advisory_gates).

Revision ID: 0028_advisory_gates
Revises: 0027_harness_enable_decision
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_advisory_gates"
down_revision = "0027_harness_enable_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gate_results",
        sa.Column("blocking", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "gate_results",
        sa.Column("findings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("gate_results", "findings")
    op.drop_column("gate_results", "blocking")
