"""crucible#116: promotion is per harness, the operator's decision of 2026-09-25 (ADR 0018).

`image_promotions` recorded one state per image (`default`, `retained`), so promoting an
image switched every harness it carried (C11). `harness_images` records one row per
harness: its default worker image, the version of that harness the image pins, and the
image it replaced, which a rollback returns to.

Existing state carries forward: each harness's current default (the most recent
`default` row that carries it, which is what a launch resolved to) becomes its own
default, and the most recent other row that carries it, `default` or `retained`, becomes
its previous image.
The downgrade folds the rows back into one row per image.

Revision ID: 0023_per_harness_images
Revises: 0022_first_run_setup
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_per_harness_images"
down_revision = "0022_first_run_setup"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def _carrying(rows: list[Any], harness: str, states: tuple[str, ...]) -> list[Any]:
    """The rows in `states` that carry the harness, most recent first."""
    matching = [r for r in rows if r.state in states and harness in (r.harnesses or {})]
    matching.sort(key=lambda r: (r.updated_at, r.digest), reverse=True)
    return matching


def upgrade() -> None:
    op.create_table(
        "harness_images",
        sa.Column("harness", sa.String(64), primary_key=True),
        sa.Column("digest", sa.String(160), nullable=False),
        sa.Column("reference", sa.Text, nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("previous_digest", sa.String(160), nullable=True),
        sa.Column("previous_reference", sa.Text, nullable=True),
        sa.Column("previous_version", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(160), nullable=False),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT digest, reference, harnesses, state, updated_at, updated_by "
                "FROM image_promotions"
            )
        )
    )
    harnesses = sorted({name for row in rows for name in (row.harnesses or {})})
    for harness in harnesses:
        defaults = _carrying(rows, harness, ("default",))
        if not defaults:
            continue
        current = defaults[0]
        # What the current default replaced for this harness: the most recent other row
        # that carries it, whether it went `retained` or stayed the default of the other
        # harnesses a narrower image was promoted over.
        previous = next(
            (r for r in _carrying(rows, harness, ("default", "retained")) if r is not current),
            None,
        )
        connection.execute(
            sa.text(
                "INSERT INTO harness_images (harness, digest, reference, version, "
                "previous_digest, previous_reference, previous_version, reason, updated_at, "
                "updated_by) VALUES (:harness, :digest, :reference, :version, :pdigest, "
                ":preference, :pversion, :reason, :updated_at, :updated_by)"
            ),
            {
                "harness": harness,
                "digest": current.digest,
                "reference": current.reference,
                "version": str(current.harnesses[harness]),
                "pdigest": previous.digest if previous is not None else None,
                "preference": previous.reference if previous is not None else None,
                "pversion": str(previous.harnesses[harness]) if previous is not None else None,
                "reason": "carried forward when promotion became per harness (crucible#116)",
                "updated_at": current.updated_at,
                "updated_by": current.updated_by,
            },
        )
    op.drop_table("image_promotions")


def downgrade() -> None:
    # One row per image again: an image that is some harness's default is `default`
    # with every harness it is the default for, and a previous image that is nobody's
    # default is `retained`. Promotion then switches every harness an image carries.
    op.create_table(
        "image_promotions",
        sa.Column("digest", sa.String(160), primary_key=True),
        sa.Column("reference", sa.Text, nullable=False),
        sa.Column("harnesses", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(160), nullable=False),
    )
    connection = op.get_bind()
    rows = list(connection.execute(sa.text("SELECT * FROM harness_images")))
    images: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = images.setdefault(
            row.digest,
            {
                "reference": row.reference,
                "harnesses": {},
                "state": "default",
                "updated_at": row.updated_at,
                "updated_by": row.updated_by,
            },
        )
        current["harnesses"][row.harness] = row.version
        current["updated_at"] = max(current["updated_at"], row.updated_at)
    # A previous image that is also some harness's default stays that harness's default
    # only; the old shape cannot say "previous for one, default for another".
    for row in rows:
        if not row.previous_digest:
            continue
        if row.previous_digest in images:
            if images[row.previous_digest]["state"] == "retained":
                images[row.previous_digest]["harnesses"][row.harness] = row.previous_version or ""
            continue
        images[row.previous_digest] = {
            "reference": row.previous_reference,
            "harnesses": {row.harness: row.previous_version or ""},
            "state": "retained",
            "updated_at": row.updated_at,
            "updated_by": row.updated_by,
        }
    for digest, image in images.items():
        connection.execute(
            sa.text(
                "INSERT INTO image_promotions (digest, reference, harnesses, state, reason, "
                "updated_at, updated_by) VALUES (:digest, :reference, CAST(:harnesses AS "
                "jsonb), :state, :reason, :updated_at, :updated_by)"
            ),
            {
                "digest": digest,
                "reference": image["reference"],
                "harnesses": json.dumps(image["harnesses"]),
                "state": image["state"],
                "reason": "folded back from per-harness defaults",
                "updated_at": image["updated_at"],
                "updated_by": image["updated_by"],
            },
        )
    op.drop_table("harness_images")
