"""Record the effective model settings each attempt was launched with.

Revision ID: 0039_attempt_effective_settings
Revises: 0038_attempt_routing_version

hades #388: Hermes is told the context length, the response allowance the gateway
reserves and the routing entry's thinking setting. They are resolved when an attempt is
launched and kept on the attempt, so a later spec of the same attempt (a collect after a
restart) uses what the worker was started with, not a newer saved value. Attempts that
exist when this runs keep NULL: what they were launched with was not recorded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039_attempt_effective_settings"
down_revision = "0038_attempt_routing_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attempts",
        sa.Column("effective_settings", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attempts", "effective_settings")
