"""Persist the credential refresh request cursor (339).

Revision ID: 0037_cred_refresh_cursor
Revises: 0036_cred_refresh_request

The supervisor's renewer tick tracked the last-handled CREDENTIAL_REFRESH_REQUESTED
seq in process memory, so a restart replayed every historical request as pending.
This column gives that cursor a durable home on the singleton status row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_cred_refresh_cursor"
down_revision = "0036_cred_refresh_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supervisor_status", sa.Column("refresh_request_cursor", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("supervisor_status", "refresh_request_cursor")
