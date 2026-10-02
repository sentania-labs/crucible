"""Record the routing policy version each attempt was routed with.

Revision ID: 0036_attempt_routing_version
Revises: 0035_credential_renewer

hades #254: an attempt routes with the routing version in force when it is routed
(an unpinned policy reference follows the newest version), so the version used is
kept on the attempt. Existing attempts keep NULL.
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


def downgrade() -> None:
    op.drop_column("attempts", "routing_version")
