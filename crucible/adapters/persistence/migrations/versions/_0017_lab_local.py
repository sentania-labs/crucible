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
ROUTING_MARKER = "0017_lab_local"
POLICY_MARKER = "Authenticated Hermes lab-local route seeded by 0017_lab_local."


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def _seed_local_route(connection: sa.engine.Connection) -> None:
    # Clone what is in force, not the highest version: a routing draft nothing references
    # would otherwise become the new default. In force is what admin/routing.py reads, the
    # latest non-retired default-software and the routing policy it names, under whatever
    # name the operator gave it. The maxima only number the new rows.
    policy_row = connection.execute(
        sa.text(
            "SELECT document FROM policies WHERE name='default-software' "
            "AND retired_at IS NULL ORDER BY version DESC LIMIT 1"
        )
    ).scalar_one_or_none()
    if policy_row is None:
        raise RuntimeError("no default-software policy is in force; 0017 has nothing to extend")
    policy = copy.deepcopy(policy_row)
    ref = (policy.get("routing") or {}).get("policy") or {}
    if not ref.get("name") or ref.get("version") is None:
        raise RuntimeError(f"the default-software policy in force names no routing policy: {ref}")
    routing_name = str(ref["name"])
    routing_row = connection.execute(
        sa.text(
            "SELECT document FROM routing_policies "
            "WHERE name=:name AND version=:version AND retired_at IS NULL"
        ),
        {"name": routing_name, "version": int(ref["version"])},
    ).scalar_one_or_none()
    if routing_row is None:
        raise RuntimeError(f"{routing_name}/{ref['version']} is missing or retired")
    routing = copy.deepcopy(routing_row)
    routing_max = connection.execute(
        sa.text("SELECT max(version) FROM routing_policies WHERE name=:name"),
        {"name": routing_name},
    ).scalar_one()
    endpoint_url = (
        os.environ.get("CRUCIBLE_LOCAL_ENDPOINT_URL")
        or os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL")
        or None
    )
    if endpoint_url:
        validate_endpoint("local", endpoint_url)
        disabled_reason = (
            "operator enablement is required after the key is validated (0017_lab_local)"
        )
    else:
        disabled_reason = "the local endpoint environment seed is not configured (0017_lab_local)"
    routing_version = int(routing_max) + 1
    routing["version"] = routing_version
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
            "VALUES (:name, :version, CAST(:document AS jsonb), now())"
        ),
        {"name": routing_name, "version": routing_version, "document": json.dumps(routing)},
    )

    policy_max = connection.execute(
        sa.text("SELECT max(version) FROM policies WHERE name='default-software'")
    ).scalar_one()
    policy_version = int(policy_max) + 1
    policy["version"] = policy_version
    policy["description"] = POLICY_MARKER
    policy["routing"] = {"policy": {"name": routing_name, "version": routing_version}}
    connection.execute(
        sa.text(
            "INSERT INTO policies(name, version, document, created_at) "
            "VALUES ('default-software', :version, CAST(:document AS jsonb), now())"
        ),
        {"version": policy_version, "document": json.dumps(policy)},
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
    op.execute(
        "DELETE FROM policies WHERE name='default-software' "
        f"AND document ->> 'description' = '{POLICY_MARKER}'"
    )
    op.execute(
        "DELETE FROM routing_policies "
        "WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(document -> 'models') AS model "
        f"WHERE model ->> 'disabled_reason' LIKE '%{ROUTING_MARKER}%')"
    )
