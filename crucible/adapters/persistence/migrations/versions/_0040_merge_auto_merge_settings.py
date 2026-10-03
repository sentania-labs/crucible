"""Join auto-merge refusals and effective attempt settings.

Revision ID: 0040_merge_auto_merge_settings
Revises: 0039_auto_merge_refusals, 0039_attempt_effective_settings
"""

from __future__ import annotations

revision = "0040_merge_auto_merge_settings"
down_revision = ("0039_auto_merge_refusals", "0039_attempt_effective_settings")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
