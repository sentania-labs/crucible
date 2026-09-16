"""Supervisor liveness: last successful tick and last error on the status row.

Revision ID: 0002_supervisor_liveness
Revises: 0001_walking_skeleton
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_supervisor_liveness"
down_revision = "0001_walking_skeleton"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("supervisor_status", sa.Column("last_success_at", TZ, nullable=True))
    op.add_column("supervisor_status", sa.Column("last_error_at", TZ, nullable=True))
    op.add_column("supervisor_status", sa.Column("last_error", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("supervisor_status", "last_error")
    op.drop_column("supervisor_status", "last_error_at")
    op.drop_column("supervisor_status", "last_success_at")
