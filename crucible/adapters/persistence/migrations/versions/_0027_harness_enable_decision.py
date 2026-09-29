"""hades #174: enabling a harness is an administrator's decision the service stores.

`harnesses.enabled_decided` says whether an administrator has enabled or disabled the
harness through the service (the Harnesses page, the admin API or the CLI). Until one
has, the configuration entry `harnesses.<name>.enabled` is the starting value and a
launch needs it and the runtime flag both, as before; once one has, the stored decision
alone decides, with no restart, and the configuration's reason stays a warning.

Every existing row starts undecided, so an upgrade changes no harness's availability:
a harness the configuration keeps off stays off until an administrator enables it.

Revision ID: 0027_harness_enable_decision
Revises: 0026_github_app_manifest
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_harness_enable_decision"
down_revision = "0026_github_app_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harnesses",
        sa.Column("enabled_decided", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("harnesses", "enabled_decided")
