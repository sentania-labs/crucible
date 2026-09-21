"""FDY-0051 Hermes local worker, routing version 4, and harness metrics.

Revision ID: 0013_hermes_local
Revises: 0012_heartbeats
"""

from __future__ import annotations

import copy
import json
import os

import sqlalchemy as sa
from alembic import op

from crucible.ports.endpoints import validate_endpoint

revision = "0013_hermes_local"
down_revision = "0012_heartbeats"
branch_labels = None
depends_on = None

POLICY_DESCRIPTION = (
    "The default software delivery policy, version 4: version 3 plus the disabled "
    "Spark local route for Hermes."
)
ENABLED_POLICY_DESCRIPTION = (
    "The default software delivery policy, version 5: Hermes on the verified "
    "four-slot Spark local pool."
)


def _seed_v4(connection: sa.engine.Connection) -> None:
    routing = copy.deepcopy(
        connection.execute(
            sa.text("SELECT document FROM routing_policies WHERE name=:name AND version=3"),
            {"name": "default-routing"},
        ).scalar_one()
    )
    routing["version"] = 4
    endpoint_url = os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL") or None
    if endpoint_url:
        validate_endpoint("local", endpoint_url)
        reason = "enablement gate has not passed"
    else:
        reason = "CRUCIBLE_SPARK_ENDPOINT_URL is not configured"
    routing["models"].append(
        {
            "id": "gpt-oss:120b",
            "harness": "hermes",
            "endpoint": "local",
            "endpoint_url": endpoint_url,
            "capability": "mid",
            "cost": "none",
            "speed": "fast",
            "pool": "spark-local",
            "weight": 1,
            "enabled": False,
            "disabled_reason": reason,
        }
    )
    routing["pools"]["spark-local"] = {
        "window": "1h",
        "budget_units": "attempts",
        "soft_limit": 0,
        "default_cooldown_seconds": 3600,
        "max_concurrency": 4,
    }
    connection.execute(
        sa.text(
            "INSERT INTO routing_policies(name, version, document, created_at) "
            "VALUES ('default-routing', 4, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(routing)},
    )

    policy = copy.deepcopy(
        connection.execute(
            sa.text("SELECT document FROM policies WHERE name=:name AND version=3"),
            {"name": "default-software"},
        ).scalar_one()
    )
    policy["version"] = 4
    policy["description"] = POLICY_DESCRIPTION
    policy["routing"] = {"policy": {"name": "default-routing", "version": 4}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', 4, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(policy)},
    )


def _seed_v5(connection: sa.engine.Connection) -> None:
    endpoint_url = os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL") or None
    if endpoint_url is None:
        return
    validate_endpoint("local", endpoint_url)
    routing = copy.deepcopy(
        connection.execute(
            sa.text("SELECT document FROM routing_policies WHERE name=:name AND version=4"),
            {"name": "default-routing"},
        ).scalar_one()
    )
    routing["version"] = 5
    hermes = next(model for model in routing["models"] if model["harness"] == "hermes")
    hermes["enabled"] = True
    hermes.pop("disabled_reason", None)
    connection.execute(
        sa.text(
            "INSERT INTO routing_policies(name, version, document, created_at) "
            "VALUES ('default-routing', 5, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(routing)},
    )

    policy = copy.deepcopy(
        connection.execute(
            sa.text("SELECT document FROM policies WHERE name=:name AND version=4"),
            {"name": "default-software"},
        ).scalar_one()
    )
    policy["version"] = 5
    policy["description"] = ENABLED_POLICY_DESCRIPTION
    policy["routing"] = {"policy": {"name": "default-routing", "version": 5}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', 5, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(policy)},
    )


def upgrade() -> None:
    op.add_column(
        "attempt_metrics", sa.Column("harness_duration_ms", sa.BigInteger(), nullable=True)
    )
    op.add_column("attempt_metrics", sa.Column("tool_calls", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text(
            "INSERT INTO harnesses(name, enabled, reason, session_compatibility, updated_at, "
            "updated_by) VALUES ('hermes', true, :reason, 'verified', now(), 'migration') "
            "ON CONFLICT (name) DO NOTHING"
        ).bindparams(reason="local Hermes 0.19.0 worker uses no subscription credential")
    )
    _seed_v4(op.get_bind())
    _seed_v5(op.get_bind())


def downgrade() -> None:
    op.execute("DELETE FROM policies WHERE name='default-software' AND version=5")
    op.execute("DELETE FROM routing_policies WHERE name='default-routing' AND version=5")
    op.execute("DELETE FROM policies WHERE name='default-software' AND version=4")
    op.execute("DELETE FROM routing_policies WHERE name='default-routing' AND version=4")
    op.execute("DELETE FROM harnesses WHERE name='hermes'")
    op.drop_column("attempt_metrics", "tool_calls")
    op.drop_column("attempt_metrics", "harness_duration_ms")
