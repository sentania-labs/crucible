"""C11 one worker image: a promotion covers every harness the image carries.

Before C11 each harness had its own image and a promotion row named one harness and
its version. The worker image now carries all four (the operator's decision of
2026-09-22), so the row records the harnesses as a name-to-version document. Existing
rows keep their one harness.

Revision ID: 0018_combined_worker_image
Revises: 0017_lab_local
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_combined_worker_image"
down_revision = "0017_lab_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("image_promotions", sa.Column("harnesses", JSONB, nullable=True))
    op.execute(
        "UPDATE image_promotions SET harnesses = jsonb_build_object(harness, harness_version)"
    )
    op.alter_column("image_promotions", "harnesses", nullable=False)
    op.drop_column("image_promotions", "harness_version")
    op.drop_column("image_promotions", "harness")


def downgrade() -> None:
    # A combined image cannot be one row per harness in the old shape. The downgrade
    # keeps the row under the alphabetically first harness it carries, which is enough
    # for the old code to read it; re-promote per-harness images after downgrading.
    op.add_column("image_promotions", sa.Column("harness", sa.String(32), nullable=True))
    op.add_column("image_promotions", sa.Column("harness_version", sa.String(32), nullable=True))
    op.execute(
        "UPDATE image_promotions SET (harness, harness_version) = ("
        "SELECT key, value FROM jsonb_each_text(harnesses) ORDER BY key LIMIT 1)"
    )
    op.execute(
        "UPDATE image_promotions SET harness = '', harness_version = '' WHERE harness IS NULL"
    )
    op.alter_column("image_promotions", "harness", nullable=False)
    op.alter_column("image_promotions", "harness_version", nullable=False)
    op.drop_column("image_promotions", "harnesses")
