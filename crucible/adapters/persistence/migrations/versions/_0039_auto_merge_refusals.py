"""Persist auto-merge refusals and retry state.

Revision ID: 0039_auto_merge_refusals
Revises: 0038_attempt_routing_version
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0036_cred_refresh_request import (
    _event_kinds as _previous_event_kinds,
)

revision = "0039_auto_merge_refusals"
down_revision = "0038_attempt_routing_version"
branch_labels = None
depends_on = None

EVENT_KINDS = ("auto_merge_updated",)


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("observed_head_sha", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "pull_requests",
        sa.Column("observed_base_ref", sa.String(length=255), nullable=False, server_default=""),
    )
    op.execute(
        "UPDATE pull_requests SET observed_head_sha = head_sha, observed_base_ref = base_ref"
    )
    op.add_column(
        "pull_requests",
        sa.Column("mergeable_state", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column("pull_requests", sa.Column("merge_refusal_cause", sa.Text(), nullable=True))
    op.add_column(
        "pull_requests", sa.Column("merge_refusal_head_sha", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "pull_requests", sa.Column("merge_refusal_base_ref", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "pull_requests",
        sa.Column("merge_refusal_mergeable_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("merge_refusal_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pull_requests", sa.Column("merge_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column("pull_requests", "mergeable_state", server_default=None)
    op.alter_column("pull_requests", "observed_base_ref", server_default=None)
    op.alter_column("pull_requests", "observed_head_sha", server_default=None)
    op.alter_column("pull_requests", "merge_refusal_count", server_default=None)
    _replace_event_kinds(_event_kinds())


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    _replace_event_kinds(_previous_event_kinds())
    for column in (
        "merge_retry_at",
        "merge_refusal_count",
        "merge_refusal_mergeable_state",
        "merge_refusal_base_ref",
        "merge_refusal_head_sha",
        "merge_refusal_cause",
        "mergeable_state",
        "observed_base_ref",
        "observed_head_sha",
    ):
        op.drop_column("pull_requests", column)
