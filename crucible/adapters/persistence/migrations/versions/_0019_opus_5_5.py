"""C11 Opus 5.5 in the Claude Code pool, disabled until the operator enables it.

The operator asked for it on 2026-09-22 ("opus-5.5 should be added to our list"). The
id is the one Claude Code 2.1.280's own model catalog lists for Opus 5.5 (c11.md),
not typed from memory. It joins the anthropic-sub pool at the frontier tier beside
claude-fable-5-1, and it is disabled: the operator enables it from the admin UI's
Routing page by uploading a routing version with the entry enabled and then a
delivery policy version that names that routing version.

The admin UI and API mint new routing and delivery policy versions on every save
(max + 1), so this does not assume a version number. It copies the routing policy the
delivery policy in force names, appends the entry, and writes both at the next free
versions.

Revision ID: 0019_opus_5_5
Revises: 0018_combined_worker_image
"""

from __future__ import annotations

import copy
import json

import sqlalchemy as sa
from alembic import op

revision = "0019_opus_5_5"
down_revision = "0018_combined_worker_image"
branch_labels = None
depends_on = None

MODEL_ID = "claude-opus-5-5"
DISABLED_REASON = "added disabled in C11; the operator enables it from the admin UI"
MARKER = "Claude Opus 5.5 added to the Claude Code pool, disabled (C11)."


def _opus_entry() -> dict[str, object]:
    return {
        "id": MODEL_ID,
        "harness": "claude_code",
        "endpoint": "subscription",
        "capability": "frontier",
        "cost": "high",
        "speed": "medium",
        "pool": "anthropic-sub",
        "weight": 1,
        "enabled": False,
        "disabled_reason": DISABLED_REASON,
    }


def upgrade() -> None:
    connection = op.get_bind()
    active = connection.execute(
        sa.text(
            "SELECT version, document FROM policies WHERE name='default-software' "
            "AND retired_at IS NULL ORDER BY version DESC LIMIT 1"
        )
    ).first()
    if active is None:
        return
    policy = copy.deepcopy(active.document)
    reference = (policy.get("routing") or {}).get("policy") or {}
    routing_name = str(reference.get("name", ""))
    found = connection.execute(
        sa.text(
            "SELECT document, retired_at FROM routing_policies "
            "WHERE name=:name AND version=:version"
        ),
        {"name": routing_name, "version": int(reference.get("version", 0))},
    ).first()
    # A retired routing version is not copied back into force.
    if found is None or found.retired_at is not None:
        return
    routing = copy.deepcopy(found.document)
    models = list(routing.get("models") or [])
    if any(model.get("id") == MODEL_ID for model in models):
        return
    # Beside the existing frontier entry of the pool, so the document reads in tiers.
    after = max(
        (
            index
            for index, model in enumerate(models)
            if model.get("harness") == "claude_code" and model.get("capability") == "frontier"
        ),
        default=len(models) - 1,
    )
    models.insert(after + 1, _opus_entry())
    routing["models"] = models
    routing_version = (
        connection.execute(
            sa.text("SELECT max(version) FROM routing_policies WHERE name=:name"),
            {"name": routing_name},
        ).scalar_one()
        + 1
    )
    routing["version"] = routing_version
    connection.execute(
        sa.text(
            "INSERT INTO routing_policies(name, version, document, created_at) "
            "VALUES (:name, :version, CAST(:document AS jsonb), now())"
        ),
        {"name": routing_name, "version": routing_version, "document": json.dumps(routing)},
    )
    policy_version = (
        connection.execute(
            sa.text("SELECT max(version) FROM policies WHERE name='default-software'")
        ).scalar_one()
        + 1
    )
    policy["version"] = policy_version
    policy["description"] = f"{policy.get('description', '')} {MARKER}".strip()
    policy["routing"] = {"policy": {"name": routing_name, "version": routing_version}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', :version, CAST(:document AS jsonb), now())"
        ),
        {"version": policy_version, "document": json.dumps(policy)},
    )


def downgrade() -> None:
    # Only the version this migration wrote: the lowest one carrying the marker that is
    # not retired (an earlier downgrade may have retired one). A copy the operator
    # uploaded later keeps the description verbatim and is left alone.
    # A version a task was submitted against is retired, not deleted, as 0011 keeps
    # the rows tasks reference: the task still resolves its policy and routing, and the
    # version before it is back in force. The routing version is kept, unchanged, while
    # any policy, retired or not, still names it.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT version, document FROM policies WHERE name='default-software' "
            "AND retired_at IS NULL AND document ->> 'description' LIKE :marker "
            "ORDER BY version LIMIT 1"
        ),
        {"marker": f"%{MARKER}"},
    ).all()
    for row in rows:
        reference = (row.document.get("routing") or {}).get("policy") or {}
        routing = {
            "name": str(reference.get("name", "")),
            "version": int(reference.get("version", 0)),
        }
        connection.execute(
            sa.text(
                "UPDATE policies p SET retired_at = now() "
                "WHERE p.name='default-software' AND p.version=:version AND EXISTS (SELECT 1 FROM tasks t "
                "WHERE t.policy_name=p.name AND t.policy_version=p.version)"
            ),
            {"version": row.version},
        )
        connection.execute(
            sa.text(
                "DELETE FROM policies p WHERE p.name='default-software' AND p.version=:version "
                "AND NOT EXISTS (SELECT 1 FROM tasks t "
                "WHERE t.policy_name=p.name AND t.policy_version=p.version)"
            ),
            {"version": row.version},
        )
        connection.execute(
            sa.text(
                "DELETE FROM routing_policies r WHERE r.name=:name AND r.version=:version "
                "AND NOT EXISTS (SELECT 1 FROM policies p "
                "WHERE p.document->'routing'->'policy'->>'name'=r.name "
                "AND (p.document->'routing'->'policy'->>'version')::int=r.version)"
            ),
            routing,
        )
