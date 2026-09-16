"""Idempotency keys are reserved inside the mutation transaction, so the response
columns are filled in the same transaction and may be null only transiently.

Revision ID: 0003_idempotency_reservation
Revises: 0002_supervisor_liveness
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_idempotency_reservation"
down_revision = "0002_supervisor_liveness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("idempotency_keys", "response_status", existing_type=sa.Integer, nullable=True)
    op.alter_column(
        "idempotency_keys",
        "response_body",
        existing_type=sa.dialects.postgresql.JSONB,
        nullable=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM idempotency_keys WHERE response_status IS NULL")
    op.alter_column("idempotency_keys", "response_status", existing_type=sa.Integer, nullable=False)
    op.alter_column(
        "idempotency_keys",
        "response_body",
        existing_type=sa.dialects.postgresql.JSONB,
        nullable=False,
    )
