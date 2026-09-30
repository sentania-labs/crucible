"""Store administrative UI sessions on the server.

Revision ID: 0033_ui_sessions
Revises: 0032_import_discard_and_rename
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_ui_sessions"
down_revision = "0032_import_discard_and_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "principal_id",
            sa.String(length=26),
            sa.ForeignKey("principals.id"),
            nullable=False,
        ),
        sa.Column("csrf", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ui_sessions_expires_at", "ui_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ui_sessions_expires_at", table_name="ui_sessions")
    op.drop_table("ui_sessions")
