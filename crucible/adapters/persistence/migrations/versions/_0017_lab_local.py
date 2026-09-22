"""C10 authenticated lab-local Hermes route and administration events.

Revision ID: 0017_lab_local
Revises: 0016_disposition_versions
"""

from __future__ import annotations

import copy
import json
import os

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0016_disposition_versions import (
    _event_kinds as _previous_event_kinds,
)
from crucible.domain.endpoints import validate_endpoint

revision = "0017_lab_local"
down_revision = "0016_disposition_versions"
branch_labels = None
depends_on = None

EVENT_KINDS = ("credential_set", "local_endpoint_updated")
EVENT_ARCHIVE = "events_c10_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def _seed_local_route(connection: sa.engine.Connection) -> None:
    source_version = connection.execute(
        sa.text(
            "SELECT max(version) FROM routing_policies "
            "WHERE name='default-routing' AND version IN (4, 5)"
        )
    ).scalar_one()
    routing = copy.deepcopy(
        connection.execute(
            sa.text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": source_version},
        ).scalar_one()
    )
    endpoint_url = (
        os.environ.get("CRUCIBLE_LOCAL_ENDPOINT_URL")
        or os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL")
        or None
    )
    if endpoint_url:
        validate_endpoint("local", endpoint_url)
        disabled_reason = "operator enablement is required after the key is validated"
    else:
        disabled_reason = "the local endpoint environment seed is not configured"
    routing["version"] = 6
    routing["models"] = [model for model in routing["models"] if model.get("harness") != "hermes"]
    routing["models"].append(
        {
            "id": "coder",
            "harness": "hermes",
            "endpoint": "local",
            "endpoint_url": endpoint_url,
            "capability": "mid",
            "cost": "none",
            "speed": "fast",
            "pool": "lab-local",
            "weight": 1,
            "enabled": False,
            "disabled_reason": disabled_reason,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )
    routing["pools"].pop("spark-local", None)
    routing["pools"]["lab-local"] = {
        "window": "1h",
        "budget_units": "attempts",
        "soft_limit": 0,
        "default_cooldown_seconds": 3600,
        "max_concurrency": 4,
    }
    connection.execute(
        sa.text(
            "INSERT INTO routing_policies(name, version, document, created_at) "
            "VALUES ('default-routing', 6, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(routing)},
    )

    policy_source = connection.execute(
        sa.text(
            "SELECT max(version) FROM policies WHERE name='default-software' AND version IN (4, 5)"
        )
    ).scalar_one()
    policy = copy.deepcopy(
        connection.execute(
            sa.text(
                "SELECT document FROM policies WHERE name='default-software' AND version=:version"
            ),
            {"version": policy_source},
        ).scalar_one()
    )
    policy["version"] = 6
    policy["description"] = (
        "The default software delivery policy, version 6: authenticated Hermes on the "
        "disabled lab-local coder route."
    )
    policy["routing"] = {"policy": {"name": "default-routing", "version": 6}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', 6, CAST(:document AS jsonb), now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"document": json.dumps(policy)},
    )


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
    connection = op.get_bind()
    archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{EVENT_ARCHIVE}"}
    ).scalar()
    if archive:
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
        op.execute(f"INSERT INTO events SELECT * FROM {EVENT_ARCHIVE}")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")
    _seed_local_route(connection)


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
    op.execute("DELETE FROM policies WHERE name='default-software' AND version=6")
    op.execute("DELETE FROM routing_policies WHERE name='default-routing' AND version=6")
