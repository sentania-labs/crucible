"""FDY-0051 enable Hermes after the single-task and four-way Spark gates passed.

Revision ID: 0014_enable_hermes_local
Revises: 0013_hermes_local
"""

from __future__ import annotations

import copy
import json
import os

import sqlalchemy as sa
from alembic import op

from crucible.ports.endpoints import validate_endpoint

revision = "0014_enable_hermes_local"
down_revision = "0013_hermes_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    endpoint_url = os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL") or None
    if endpoint_url is None:
        return
    validate_endpoint("local", endpoint_url)
    connection = op.get_bind()
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
    policy["description"] = (
        "The default software delivery policy, version 5: Hermes on the verified "
        "four-slot Spark local pool."
    )
    policy["routing"] = {"policy": {"name": "default-routing", "version": 5}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', 5, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(policy)},
    )


def downgrade() -> None:
    op.execute("DELETE FROM policies WHERE name='default-software' AND version=5")
    op.execute("DELETE FROM routing_policies WHERE name='default-routing' AND version=5")
