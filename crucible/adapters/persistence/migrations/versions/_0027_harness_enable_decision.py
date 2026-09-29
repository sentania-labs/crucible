"""hades #174: enabling a harness is an administrator's decision the service stores.

`harnesses.enabled_decided` says whether an administrator has enabled or disabled the
harness through the service (the Harnesses page, the admin API or the CLI). Until one
has, the configuration entry `harnesses.<name>.enabled` is the starting value and a
launch needs it and the runtime flag both, as before; once one has, the stored decision
alone decides, with no restart, and the configuration's reason stays a warning.

A row an administrator disabled (or a credential removal did) is recorded as that
decision. The row's provenance tells the two apart: every service change stamps
`updated_by` with its principal, and only the seeds (0008, 0013) carry `migration`. A
seeded row is no one's decision, so it starts undecided with the runtime flag on and the
configuration default governs it, as ADR 0021 says: Codex, seeded off in 0008, is off
because the configuration keeps it off, not because an administrator disabled it, and
a later change to its configuration entry still applies. Every enabled row starts
undecided too. With the shipped configuration no harness's availability changes.

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

# What 0008 and 0013 stamp on the rows they seed; the service stamps its principal.
SEEDED_BY = "migration"
# 0008's reason for the Codex seed, restored on a downgrade.
CODEX_SEED_REASON = (
    "unverified: the dedicated session's Crucible-side token refresh has not yet been "
    "observed (S1b step 5), so the operator's session cannot be confirmed after it (step 6)"
)


def upgrade() -> None:
    op.add_column(
        "harnesses",
        sa.Column("enabled_decided", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text(
            "UPDATE harnesses SET enabled_decided = true "
            "WHERE NOT enabled AND updated_by <> :seeded"
        ).bindparams(seeded=SEEDED_BY)
    )
    op.execute(
        sa.text(
            "UPDATE harnesses SET enabled = true, reason = '' "
            "WHERE NOT enabled AND updated_by = :seeded"
        ).bindparams(seeded=SEEDED_BY)
    )


def downgrade() -> None:
    # A seeded row no one has touched since goes back to its 0008 seed, so the two
    # gates that apply again below 0027 see what they saw before.
    op.execute(
        sa.text(
            "UPDATE harnesses SET enabled = false, reason = :reason "
            "WHERE name = 'codex' AND updated_by = :seeded AND NOT enabled_decided"
        ).bindparams(reason=CODEX_SEED_REASON, seeded=SEEDED_BY)
    )
    op.drop_column("harnesses", "enabled_decided")
