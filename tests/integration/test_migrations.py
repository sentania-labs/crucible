from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, text

from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import make_engine
from crucible.contracts.policy import RoutingPolicyV1
from tests.fixtures import contract_document

pytestmark = pytest.mark.integration


def test_up_down_up_from_empty(database_url: str) -> None:
    migrate.downgrade(database_url, "base")
    engine = make_engine(database_url)
    assert (
        inspect(engine).get_table_names() == ["alembic_version"]
        or "tasks" not in inspect(engine).get_table_names()
    )
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert {
        "principals",
        "repositories",
        "policies",
        "tasks",
        "task_contracts",
        "executions",
        "attempts",
        "events",
        "leases",
        "supervisor_status",
        "completion_claims",
        "heartbeats",
    } <= names
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    with engine.connect() as conn:
        # The migrations seed versions 1 through 4 of default-software.
        assert conn.execute(text("SELECT count(*) FROM policies")).scalar() == 4
    migrate.downgrade(database_url, "base")
    assert "tasks" not in inspect(engine).get_table_names()
    migrate.upgrade(database_url)
    engine.dispose()


def test_events_and_contracts_are_append_only(migrated: str) -> None:
    engine = make_engine(migrated)
    marker = "append-only-probe"
    with engine.begin() as conn:
        before = conn.execute(text("SELECT count(*) FROM events")).scalar()
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'principal_created', :marker, true, '{}')"
            ),
            {"marker": marker},
        )
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("UPDATE events SET kind = 'task_submitted'"))
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM events"))
    with engine.connect() as conn:
        # The refusals changed nothing, and the row this test wrote is still there. The
        # table is not asserted empty: a downgrade past C4 now preserves its events
        # rather than deleting them, so an earlier test can legitimately leave rows.
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == (before or 0) + 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE principal = :marker"), {"marker": marker}
            ).scalar()
            == 1
        )
    engine.dispose()


def test_unknown_event_kind_is_rejected(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.begin() as conn, pytest.raises(Exception, match="ck_events_kind"):
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'made_up_kind', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_fresh_schema_has_no_drift(migrated: str) -> None:
    engine = make_engine(migrated)
    assert migrate.schema_drift(engine) is None
    ok, detail = migrate.is_current(engine, migrated)
    assert ok and "schema matches" in detail
    engine.dispose()


def test_0004_creates_the_c2_tables_and_seeds_the_routing_policy(migrated: str) -> None:
    engine = make_engine(migrated)
    names = set(inspect(engine).get_table_names())
    assert {
        "routing_policies",
        "artifacts",
        "evidence",
        "review_reports",
        "gate_results",
        "acceptance_results",
        "escalations",
        "decisions",
        "review_dispositions",
        "wakes",
        "attempt_metrics",
    } <= names
    with engine.connect() as conn:
        # 0004 seeds version 1; 0008 adds version 2 with the model ids the C5 live runs
        # verified; 0011 adds version 3 and 0013 adds the disabled Hermes route in 4.
        versions = (
            conn.execute(
                text(
                    "SELECT version FROM routing_policies WHERE name = 'default-routing' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        assert versions == [1, 2, 3, 4]
        # Later revisions add immutable policy versions that name their matching
        # routing version (05b).
        policy_versions = conn.execute(
            text(
                "SELECT version, document -> 'routing' -> 'policy' ->> 'version' "
                "FROM policies WHERE name = 'default-software' ORDER BY 1"
            )
        ).all()
        assert [(v, int(r)) for v, r in policy_versions] == [
            (1, 1),
            (2, 2),
            (3, 3),
            (4, 4),
        ]
        routing = conn.execute(
            text(
                "SELECT document -> 'routing' FROM policies "
                "WHERE name = 'default-software' AND version = 1"
            )
        ).scalar()
    assert routing == {"policy": {"name": "default-routing", "version": 1}}
    engine.dispose()


def test_0013_records_an_unconfigured_spark_route_with_a_reason(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.connect() as conn:
        document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=4")
        ).scalar_one()
        assert (
            conn.execute(text("SELECT count(*) FROM harnesses WHERE name='hermes'")).scalar() == 1
        )
    routing = RoutingPolicyV1.model_validate(document)
    hermes = routing.model("gpt-oss:120b")
    assert hermes is not None and not hermes.enabled and hermes.endpoint_url is None
    assert hermes.disabled_reason == "CRUCIBLE_SPARK_ENDPOINT_URL is not configured"
    assert routing.pools["spark-local"].max_concurrency == 4
    engine.dispose()


def test_0013_materializes_the_configured_spark_url(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.downgrade(database_url, "0012_heartbeats")
    monkeypatch.setenv("CRUCIBLE_SPARK_ENDPOINT_URL", "http://192.0.2.41:11434/v1")
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.connect() as conn:
        document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=4")
        ).scalar_one()
    hermes = RoutingPolicyV1.model_validate(document).model("gpt-oss:120b")
    assert hermes is not None and hermes.endpoint_url == "http://192.0.2.41:11434/v1"
    assert hermes.disabled_reason == "enablement gate has not passed"
    with engine.connect() as conn:
        enabled_document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=5")
        ).scalar_one()
        policy_ref = conn.execute(
            text(
                "SELECT document -> 'routing' -> 'policy' ->> 'version' FROM policies "
                "WHERE name='default-software' AND version=5"
            )
        ).scalar_one()
    enabled = RoutingPolicyV1.model_validate(enabled_document).model("gpt-oss:120b")
    assert enabled is not None and enabled.enabled and enabled.disabled_reason is None
    assert enabled.endpoint_url == "http://192.0.2.41:11434/v1"
    assert int(policy_ref) == 5
    engine.dispose()


def test_0004_down_and_up(database_url: str) -> None:
    """Down migrations are required for every revision in v0.x (14)."""
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0003_idempotency_reservation")
    names = set(inspect(engine).get_table_names())
    assert "gate_results" not in names and "wakes" not in names
    assert "head_sha" not in {c["name"] for c in inspect(engine).get_columns("tasks")}
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert {"gate_results", "wakes", "attempt_metrics"} <= names
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


def test_0002_down_and_up(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0001_walking_skeleton")
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert "last_success_at" not in cols
    migrate.upgrade(database_url)
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert {"last_success_at", "last_error_at", "last_error"} <= cols
    engine.dispose()


def test_a_downgrade_past_c4_keeps_the_c4_events(database_url: str) -> None:
    """14: `events` is the audit log. The revisions below 0007 recreate the event-kind
    CHECK in its validating form, which C4 rows cannot satisfy, so the downgrade moves
    them aside rather than deleting them, and the upgrade moves them back."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'publish_completed', 'tests', true, "
                '\'{"marker": "downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0006_log_occurrence")
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind = 'publish_completed'")
            ).scalar()
            == 0
        )
        # Not deleted: moved.
        kept = conn.execute(
            text(
                "SELECT count(*) FROM events_c4_archive WHERE payload->>'marker' = 'downgrade-test'"
            )
        ).scalar()
    assert kept == 1
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'downgrade-test'")
        ).scalar()
        assert conn.execute(text("SELECT to_regclass('public.events_c4_archive')")).scalar() is None
    assert restored == 1
    # And the sequence still hands out a usable value after the rows came back.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'publish_completed', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_the_publish_pending_flag_becomes_the_publishing_state(database_url: str) -> None:
    """09: C4 replaces the flag with the state. A task accepted under C2 is waiting with
    the flag raised, and a bare drop would strand it in `awaiting_acceptance` for ever."""
    migrate.downgrade(database_url, "0006_log_occurrence")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (id, name, role, token_salt, token_hash, created_at) "
                "VALUES ('01MIGP00000000000000000001', 'migration-test', 'orchestrator', "
                "'\\x00', '\\x00', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO repositories (id, name, url, default_branch, policy_name, "
                "registered_by, created_at) VALUES ('01MIGR00000000000000000001', "
                "'migration/test', 'https://github.com/migration/test', 'main', "
                "'default-software', 'tests', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, external_id, principal_id, repository_id, project, "
                "title, state, contract_version, policy_name, policy_version, created_at, "
                "updated_at, head_sha, publish_pending) VALUES "
                "('01MIGT00000000000000000001', 'MIG-1', '01MIGP00000000000000000001', "
                "'01MIGR00000000000000000001', 'p', 't', 'awaiting_acceptance', 1, "
                "'default-software', 1, now(), now(), 'abc123', true)"
            )
        )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM tasks WHERE id = '01MIGT00000000000000000001'")
        ).scalar_one()
        events = conn.execute(
            text(
                "SELECT count(*) FROM events WHERE task_id = '01MIGT00000000000000000001' "
                "AND kind = 'task_publishing'"
            )
        ).scalar_one()
    assert state == "publishing"
    assert events == 1
    engine.dispose()


def test_0010_down_and_up(database_url: str) -> None:
    """C6: the bootstrap_imports table and the attempts.unsupervised flag (15, 14)."""
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert "bootstrap_imports" in names
    assert "unsupervised" in {c["name"] for c in inspect(engine).get_columns("attempts")}
    indexes = {i["name"] for i in inspect(engine).get_indexes("bootstrap_imports")}
    assert {"ix_bootstrap_imports_content", "uq_bootstrap_imports_authoritative"} <= indexes
    migrate.downgrade(database_url, "0009_administration")
    names = set(inspect(engine).get_table_names())
    assert "bootstrap_imports" not in names
    assert "unsupervised" not in {c["name"] for c in inspect(engine).get_columns("attempts")}
    with engine.begin() as conn, pytest.raises(Exception, match="ck_events_kind"):
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'bootstrap_handoff', 'tests', true, '{}')"
            )
        )
    migrate.upgrade(database_url)
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'bootstrap_handoff', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_0011_down_and_up_preserves_class_routing_events(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'pool_exhausted', 'tests', true, "
                '\'{"marker": "c6b-downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0010_bootstrap_import")
    names = set(inspect(engine).get_table_names())
    assert "pool_exhaustions" not in names
    assert "resume_at" not in {c["name"] for c in inspect(engine).get_columns("tasks")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('public.events_c6b_archive')")).scalar()
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'c6b-downgrade-test'")
        ).scalar_one()
        assert (
            conn.execute(text("SELECT to_regclass('public.events_c6b_archive')")).scalar() is None
        )
    assert restored == 1
    engine.dispose()


def test_0012_down_and_up_preserves_stall_events(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'worker_stalled', 'tests', true, "
                '\'{"marker": "c6c-downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0011_class_routing")
    assert "heartbeats" not in set(inspect(engine).get_table_names())
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'c6c-downgrade-test'")
        ).scalar_one()
    assert restored == 1
    engine.dispose()


def test_0011_refuses_an_incompatible_contract_on_a_non_terminal_task(
    database_url: str,
) -> None:
    engine = make_engine(database_url)
    if migrate.current_revision(engine) is None:
        migrate.upgrade(database_url, "0010_bootstrap_import")
    else:
        migrate.downgrade(database_url, "0010_bootstrap_import")
    document = contract_document(external_id="MIG-C6B-GUARD")
    document["execution_request"].update({"harness": "codex", "model": "gpt-5.6-luna"})
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (id, name, role, token_salt, token_hash, created_at) "
                "VALUES ('01MIGC6BP0000000000000001', 'c6b-migration-principal', "
                "'orchestrator', '\\x00', '\\x00', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO repositories (id, name, url, default_branch, policy_name, "
                "registered_by, created_at, external_review_attested) VALUES "
                "('01MIGC6BR0000000000000001', "
                "'migration/c6b', 'https://example.invalid/migration/c6b', 'main', "
                "'default-software', 'tests', now(), false) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, external_id, principal_id, repository_id, project, "
                "title, state, contract_version, policy_name, policy_version, created_at, "
                "updated_at, head_sha) VALUES ('01MIGC6BT0000000000000001', 'MIG-C6B-GUARD', "
                "'01MIGC6BP0000000000000001', '01MIGC6BR0000000000000001', 'p', 't', "
                "'submitted', 1, 'default-software', 1, now(), now(), NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO task_contracts "
                "(id, task_id, version, document, sha256, submitted_at) VALUES "
                "('01MIGC6BC0000000000000001', '01MIGC6BT0000000000000001', 1, "
                "CAST(:document AS jsonb), 'invalid-contract-for-migration-guard', now())"
            ),
            {"document": json.dumps(document)},
        )
    with pytest.raises(RuntimeError, match="01MIGC6BT0000000000000001"):
        migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET state='cancelled' WHERE id='01MIGC6BT0000000000000001'")
        )
    migrate.upgrade(database_url)
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()
