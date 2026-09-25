"""crucible#118: the last harness test, per harness.

A harness test runs the path a real task takes (the harness's image, its credential,
worker egress, one minimal model call) and reports each step in plain words. The last
result is kept on the harness's row so the Harnesses page shows it beside the harness.

Revision ID: 0024_harness_test
Revises: 0023_per_harness_images
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_harness_test"
down_revision = "0023_per_harness_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("harnesses", sa.Column("last_test", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("harnesses", "last_test")
