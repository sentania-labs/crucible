"""crucible#157, ADR 0019: a repository may be private.

A private repository is cloned with a read-only GitHub App installation token, minted
for its preparation step and discarded when the step ends. Every repository registered
before this revision was cloned with no credential, so each one is public.

Revision ID: 0025_private_checkout
Revises: 0024_harness_test
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_private_checkout"
down_revision = "0024_harness_test"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("private", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.alter_column("repositories", "private", server_default=None)


def downgrade() -> None:
    op.drop_column("repositories", "private")
