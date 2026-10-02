"""Empty migration because no columns remain.

Revision ID: 0037_attempt_routing_policy
Revises: 0036_attempt_routing_version
"""

from __future__ import annotations

revision = "0037_attempt_routing_policy"
down_revision = "0036_attempt_routing_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
