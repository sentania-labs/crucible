"""The bootstrap ledger handoff against real PostgreSQL (15, 18): the full import and
commit lifecycle through the API and through `crucible-admin`, idempotence, the
partial-failure rollback, the refusals, the supervisor leaving the unsupervised attempt
alone, and a scripted Foundry start-of-session that reconstructs the live-task set
from the API alone. Bundles are synthetic (tests/fixtures.py) or the one
`foundry-ledger` itself wrote for its own invented fixture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin import bootstrap
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from crucible.client.config import ADMIN_TOKEN_ENV
from crucible.client.http import Api
from crucible.domain.bootstrap import SOURCE_CLOSED_STATES, STATE_MAP
from tests.admin_cli import admin_main, envelope_data
from tests.fixtures import bootstrap_event, bootstrap_task, synthetic_bundle
from tests.unit.test_bootstrap_bundle import producer_bundle

pytestmark = pytest.mark.integration

OWNER = "orchestrator-principal"


@pytest.fixture(autouse=True)
def _quiet_cli_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)


@pytest.fixture
def admin_ctx(ctx: AppContext, provider: FakeProvider, tmp_path: Path) -> AdminContext:
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
    )
    ctx.admin = admin
    return admin


@pytest.fixture
def live_supervisor(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    """On the system clock, so the lease reads as live to the API (fake clock) and to
    the CLI (system clock) alike."""
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="bootstrap-tests",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )


@pytest.fixture
def admin_client(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext, live_supervisor: Supervisor
) -> Iterator[TestClient]:
    asyncio.run(live_supervisor.tick())
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


@pytest.fixture
def config_file(migrated: str, tmp_path: Path) -> Path:
    path = tmp_path / "crucible.toml"
    path.write_text(
        "\n".join(
            [
                "[database]",
                f'url = "{migrated}"',
                "[supervisor]",
                "lease_ttl_seconds = 300",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def run_cli(config: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> Any:
    admin_main(["--config", str(config), *argv])
    return envelope_data(capsys)


def bundle_file(tmp_path: Path, bundle: dict[str, Any], name: str = "crucible.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def submit(client: TestClient, bundle: dict[str, Any], *, reason: str = "handoff") -> Any:
    return client.post(
        "/v1/import/bootstrap", params={"reason": reason, "owner": OWNER}, json=bundle
    )


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)
            for table in ("tasks", "executions", "attempts", "events", "bootstrap_imports")
        }


# ----- the lifecycle -----------------------------------------------------------------


def test_import_writes_every_record_and_reports_it(
    admin_client: TestClient, engine: Engine
) -> None:
    raw = synthetic_bundle()
    before = counts(engine)
    response = submit(admin_client, raw)
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["state"] == "verified"
    assert report["content_sha256"] == raw["content_sha256"]
    assert report["source_migrated"] is None and report["source"] == raw["source"]
    assert report["principal"] == OWNER and report["imported_by"] == "admin-principal"
    assert report["counts"] == {
        "tasks": 8,
        "events": 15,
        "executions": 2,
        "attempts": 2,
        "handoff_events": 0,
    }
    assert report["state_map"] == {
        state: {"target": STATE_MAP[state].value, "count": 1} for state in STATE_MAP
    }
    # The synthetic tasks name a registered repository, so none is on the sentinel.
    assert report["repositories"] == {"matched": {"example-service": 8}, "sentinel": []}
    after = counts(engine)
    assert after["tasks"] - before["tasks"] == 8
    assert after["executions"] - before["executions"] == 2
    assert after["attempts"] - before["attempts"] == 2
    assert after["bootstrap_imports"] - before["bootstrap_imports"] == 1
    # 15 step 3: external ids preserved, timestamps in UTC, closed tasks closed.
    for entry in report["tasks"]:
        view = admin_client.get(f"/v1/tasks/{entry['task_id']}").json()
        assert view["external_id"] == entry["external_id"]
        assert view["state"] == entry["state"] and view["principal"] == OWNER
        assert view["created_at"] == "2026-09-01T14:00:00.000000+00:00"
        assert view["updated_at"] == "2026-09-02T15:30:00.000000+00:00"
        assert (view["closed_at"] is not None) is (entry["state"] in ("closed", "cancelled"))
        assert view["contract"] == {} and view["contract_version"] == 0
        if entry["unsupervised"]:
            assert view["executions"][0]["provider"] == "bootstrap"
            assert view["executions"][0]["state"] == "active"
            assert view["latest_attempt"]["state"] == "running"
        else:
            assert view["executions"] == []
        events = admin_client.get(f"/v1/tasks/{entry['task_id']}/events").json()["items"]
        imported = [e for e in events if e["kind"] == "bootstrap_event_imported"]
        source_events = [e for e in raw["events"] if e["task"] == entry["external_id"]]
        # Each source event kept in full: original seq, original ts string, event, who,
        # detail; a new global seq on the Crucible row; unverified, because Crucible did
        # not observe it.
        assert [
            {k: e["payload"][k] for k in ("seq", "ts", "task", "event", "who", "detail")}
            for e in imported
        ] == source_events
        assert all(e["verified"] is False for e in imported)
        assert [e["seq"] for e in imported] == sorted(e["seq"] for e in imported)
        assert imported[0]["ts"] == "2026-09-01T14:00:00.000000+00:00"
        record = next(e for e in events if e["kind"] == "bootstrap_task_imported")
        assert record["payload"]["record"] == next(
            t for t in raw["tasks"] if t["id"] == entry["external_id"]
        )
    # The diff of what the columns could not carry, per task and for the events.
    fields = {u["field"] for u in report["uncarried"]["tasks"]}
    assert fields == {"scope", "objective", "contract", "model", "harness", "execution", "refs"}
    assert {u["field"] for u in report["uncarried"]["events"]} == {
        "seq",
        "ts",
        "task",
        "event",
        "who",
        "detail",
    }
    # Global event seq values are new and increasing across the whole import.
    assert report["events"]["first_seq"] < report["events"]["last_seq"]
    # Show and list say the same thing; the audit has the verification.
    assert admin_client.get(f"/v1/import/bootstrap/{report['import_id']}").json() == report
    listed = admin_client.get("/v1/import/bootstrap").json()["items"]
    assert [i["import_id"] for i in listed] == [report["import_id"]]
    audit = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    verified = [e for e in audit if e["kind"] == "bootstrap_import_verified"]
    assert len(verified) == 1 and verified[0]["payload"]["reason"] == "handoff"
    assert verified[0]["payload"]["after"]["import"] == report["import_id"]
    status = admin_client.get("/v1/admin/status").json()["bootstrap"]
    assert status["authoritative"] is None
    assert status["imports"][0]["state"] == "verified"


def test_commit_makes_the_import_authoritative_and_records_the_handoff_on_every_task(
    admin_client: TestClient,
) -> None:
    report = submit(admin_client, synthetic_bundle()).json()
    import_id = report["import_id"]
    refused = admin_client.post(f"/v1/import/bootstrap/{import_id}/commit", json={})
    assert refused.status_code == 422 and refused.json()["errors"][0]["path"] == "reason"
    response = admin_client.post(
        f"/v1/import/bootstrap/{import_id}/commit", json={"reason": "foundry hands off"}
    )
    assert response.status_code == 200, response.text
    committed = response.json()
    assert committed["state"] == "authoritative"
    assert committed["counts"]["handoff_events"] == 8
    assert committed["committed_by"] == "admin-principal"
    for entry in committed["tasks"]:
        kinds = [
            e["kind"]
            for e in admin_client.get(f"/v1/tasks/{entry['task_id']}/events").json()["items"]
        ]
        assert kinds.count("bootstrap_handoff") == 1
        # The handoff is the last thing on every task, and the task did not move.
        assert kinds[-1] == "bootstrap_handoff"
        assert admin_client.get(f"/v1/tasks/{entry['task_id']}").json()["state"] == entry["state"]
    # A second commit is refused: the import is no longer `verified`.
    again = admin_client.post(f"/v1/import/bootstrap/{import_id}/commit", json={"reason": "x"})
    assert again.status_code == 409 and "authoritative" in again.json()["detail"]
    status = admin_client.get("/v1/admin/status").json()["bootstrap"]
    assert status["authoritative"] == import_id
    audit = [
        e["kind"]
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    ]
    assert "bootstrap_import_committed" in audit and "admin_refused" in audit


def test_only_one_import_holds_authority(admin_client: TestClient) -> None:
    first = submit(admin_client, synthetic_bundle()).json()
    other = synthetic_bundle()
    for task in other["tasks"]:
        task["id"] = task["id"].replace("SYN-", "ALT-")
    for event in other["events"]:
        event["task"] = event["task"].replace("SYN-", "ALT-")
    from tests.fixtures import bootstrap_content_sha256  # noqa: PLC0415

    other["content_sha256"] = bootstrap_content_sha256(other["tasks"], other["events"])
    second = submit(admin_client, other).json()
    assert (
        admin_client.post(
            f"/v1/import/bootstrap/{first['import_id']}/commit", json={"reason": "first"}
        ).status_code
        == 200
    )
    refused = admin_client.post(
        f"/v1/import/bootstrap/{second['import_id']}/commit", json={"reason": "second"}
    )
    assert refused.status_code == 409
    assert first["import_id"] in refused.json()["detail"]
    assert "ADR 0006" in refused.json()["detail"]
    states = {
        i["import_id"]: i["state"] for i in admin_client.get("/v1/import/bootstrap").json()["items"]
    }
    assert states == {first["import_id"]: "authoritative", second["import_id"]: "verified"}


def test_the_same_bundle_twice_is_one_import(admin_client: TestClient, engine: Engine) -> None:
    raw = synthetic_bundle()
    first = submit(admin_client, raw)
    assert first.status_code == 201
    after_first = counts(engine)
    again = submit(admin_client, raw, reason="retried handoff")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert counts(engine) == after_first
    assert len(admin_client.get("/v1/import/bootstrap").json()["items"]) == 1
    # And after the commit the replay returns the authoritative report.
    admin_client.post(
        f"/v1/import/bootstrap/{first.json()['import_id']}/commit", json={"reason": "go"}
    )
    assert submit(admin_client, raw).json()["state"] == "authoritative"


def test_a_bundle_that_fails_validation_stores_nothing_and_returns_every_problem(
    admin_client: TestClient, engine: Engine
) -> None:
    before = counts(engine)
    raw = synthetic_bundle()
    raw["tasks"][1]["state"] = "rejected"
    raw["counts"]["tasks"] = 7
    raw["events"][2]["seq"] = 1
    del raw["source"]["migrated"]
    response = submit(admin_client, raw)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"].endswith("bootstrap-bundle-invalid")
    # The edits to a task and an event also move the hash, so that is the fifth.
    assert {e["path"] for e in body["errors"]} == {
        "tasks[1].state",
        "counts.tasks",
        "events[2].seq",
        "source.migrated",
        "content_sha256",
    }
    after = counts(engine)
    assert {k: v for k, v in after.items() if k != "events"} == {
        k: v for k, v in before.items() if k != "events"
    }
    # The one event is the refusal itself, which is best effort and not an import record.
    assert after["events"] == before["events"] + 1
    audit = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    assert audit[-1]["kind"] == "admin_refused"
    assert "5 problem(s)" in audit[-1]["payload"]["detail"]
    assert admin_client.get("/v1/import/bootstrap").json()["items"] == []


def test_an_external_id_the_owner_already_has_is_a_problem(
    admin_client: TestClient, client: TestClient
) -> None:
    from tests.integration.conftest import submit_and_start  # noqa: PLC0415

    submit_and_start(client, "crucible-worker:fake-succeed", external_id="SYN-0003", start=False)
    response = submit(admin_client, synthetic_bundle())
    assert response.status_code == 422
    assert [e["path"] for e in response.json()["errors"]] == ["tasks[2].id"]
    assert OWNER in response.json()["errors"][0]["message"]


def test_a_secret_shaped_value_is_refused_by_path_and_never_stored(
    admin_client: TestClient, engine: Engine
) -> None:
    from tests.fixtures import bootstrap_content_sha256  # noqa: PLC0415

    raw = synthetic_bundle()
    secret = "ghp_" + "a" * 40
    raw["events"][4]["detail"] = f"pasted by mistake: {secret}"
    raw["content_sha256"] = bootstrap_content_sha256(raw["tasks"], raw["events"])
    response = submit(admin_client, raw)
    assert response.status_code == 422
    assert [e["path"] for e in response.json()["errors"]] == ["events[4]"]
    assert secret not in response.text
    with engine.connect() as conn:
        haystack = "\n".join(
            str(v) for v in conn.execute(text("SELECT CAST(payload AS TEXT) FROM events")).scalars()
        )
    assert secret not in haystack


def test_an_unknown_owner_is_a_problem_and_the_default_owner_is_the_caller(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/v1/import/bootstrap",
        params={"reason": "x", "owner": "nobody"},
        json=synthetic_bundle(),
    )
    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"path": "principal", "message": "no principal named 'nobody'"}
    ]
    response = admin_client.post(
        "/v1/import/bootstrap", params={"reason": "x"}, json=synthetic_bundle()
    )
    assert response.status_code == 201
    assert response.json()["principal"] == "admin-principal"


def test_the_guard_applies_to_submit_and_commit(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext
) -> None:
    """No live supervisor: 503 and nothing written. A reason is optional on the submit
    and required on the commit (crucible#117)."""
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        response = submit(c, synthetic_bundle())
        assert response.status_code == 503 and response.json()["type"].endswith(
            "supervisor-not-live"
        )
        assert c.get("/v1/import/bootstrap").json()["items"] == []
        response = c.post("/v1/import/bootstrap", json=synthetic_bundle())
        assert response.status_code == 503, response.text
        response = c.post("/v1/import/bootstrap/01ABCDEFGHJKMNPQRSTVWXYZ00/commit", json={})
        assert response.status_code == 422 and response.json()["errors"][0]["path"] == "reason"
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
    ) as orchestrator:
        assert orchestrator.post("/v1/import/bootstrap", json={}).status_code == 403
        assert orchestrator.get("/v1/import/bootstrap").status_code == 403


def test_a_failure_mid_write_rolls_the_whole_import_back(
    admin_ctx: AdminContext,
    admin_client: TestClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15 step 3: one transaction. A failure after some records are written leaves
    nothing: no task, no execution, no attempt, no event, no import row."""
    from crucible.application.transitions import record_event as real  # noqa: PLC0415

    before = counts(engine)
    calls = {"n": 0}

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("simulated failure after the fourth event")
        return real(*args, **kwargs)

    monkeypatch.setattr("crucible.application.admin.bootstrap.record_event", failing)
    with admin_ctx.uow_factory() as uow, pytest.raises(RuntimeError, match="simulated"):
        bootstrap.submit(
            admin_ctx,
            uow,
            principal="admin-principal",
            bundle=synthetic_bundle(),
            reason="handoff",
            owner=OWNER,
        )
    assert calls["n"] == 5
    assert counts(engine) == before
    assert admin_client.get("/v1/import/bootstrap").json()["items"] == []


def test_the_supervisor_leaves_the_unsupervised_attempt_alone(
    admin_client: TestClient, live_supervisor: Supervisor, provider: FakeProvider
) -> None:
    report = submit(admin_client, synthetic_bundle()).json()
    running = [t for t in report["tasks"] if t["unsupervised"]]
    assert len(running) == 2
    for _ in range(3):
        asyncio.run(live_supervisor.tick())
    for entry in running:
        view = admin_client.get(f"/v1/tasks/{entry['task_id']}").json()
        assert view["state"] == "running"
        assert view["latest_attempt"]["state"] == "running"
        attempt = admin_client.get(f"/v1/attempts/{entry['attempt_id']}").json()
        assert attempt["state"] == "running" and attempt["handle"] is None
    # No provider was asked about it, no worker is listed, no cap is consumed.
    status = admin_client.get("/v1/admin/status").json()
    assert status["workers"] == []
    assert status["supervisor"]["healthy"] is True
    assert all(h["concurrency_in_use"] == 0 for h in status["harnesses"])
    supervisor = admin_client.get("/v1/supervisor").json()
    assert supervisor["counts"]["attempts_live"] == 0
    assert supervisor["counts"]["tasks_running"] == 2


def test_cancelling_an_imported_running_task_settles_without_a_worker(
    admin_client: TestClient, client: TestClient, live_supervisor: Supervisor
) -> None:
    """There is no worker to drain or kill. The execution closes, the task becomes
    cancelled, and the unsupervised attempt stays as the record of a run Crucible never
    observed."""
    report = submit(admin_client, synthetic_bundle()).json()
    entry = next(t for t in report["tasks"] if t["source_state"] == "running")
    response = client.post(
        f"/v1/tasks/{entry['task_id']}/cancel",
        json={"reason": "no longer wanted", "verbatim": "drop it", "decided_by": "operator"},
    )
    assert response.status_code == 200, response.text
    for _ in range(3):
        asyncio.run(live_supervisor.tick())
    view = admin_client.get(f"/v1/tasks/{entry['task_id']}").json()
    assert view["state"] == "cancelled"
    assert view["executions"][0]["state"] == "cancelled"
    assert view["latest_attempt"]["state"] == "running"
    kinds = [
        e["kind"] for e in admin_client.get(f"/v1/tasks/{entry['task_id']}/events").json()["items"]
    ]
    assert kinds[-2:] == ["execution_cancelled", "task_cancelled"]


# ----- the producer's own bundle -------------------------------------------------------


def test_the_bundle_foundry_ledger_wrote_imports_and_commits(
    admin_client: TestClient,
) -> None:
    """tests/fixtures_data/bootstrap/foundry_ledger_example.json is what the real
    `foundry-ledger export --format crucible` wrote for its own invented fixture. The
    repository it names (/opt/example/widget) is registered nowhere, so every task lands
    on the sentinel and the report says so."""
    raw = producer_bundle()
    response = submit(admin_client, raw)
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["counts"]["tasks"] == 4 and report["counts"]["events"] == 11
    assert report["repositories"]["matched"] == {}
    assert report["repositories"]["sentinel"] == ["EX-0001", "EX-0002", "EX-0003", "EX-0004"]
    # EX-0004 has no repository at all, so it has nothing to name as uncarried; the
    # three that name one are told it is not registered.
    assert {
        (u["external_id"], u["carried_as"])
        for u in report["uncarried"]["tasks"]
        if u["field"] == "repository"
    } == {(f"EX-000{n}", "sentinel") for n in range(1, 4)}
    for entry in report["tasks"]:
        view = admin_client.get(f"/v1/tasks/{entry['task_id']}").json()
        assert view["repository"] == "bootstrap"
    # CST and CDT both normalize: EX-0001 was created 09:00 CST (15:00 UTC).
    first = next(t for t in report["tasks"] if t["external_id"] == "EX-0001")
    view = admin_client.get(f"/v1/tasks/{first['task_id']}").json()
    assert view["created_at"] == "2026-03-01T15:00:00.000000+00:00"
    committed = admin_client.post(
        f"/v1/import/bootstrap/{report['import_id']}/commit", json={"reason": "go"}
    ).json()
    assert committed["state"] == "authoritative"


# ----- parity: the CLI drives the same services -----------------------------------------


def test_bootstrap_through_the_cli_matches_the_api(
    admin_client: TestClient,
    config_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = bundle_file(tmp_path, synthetic_bundle())
    from_cli = run_cli(
        config_file,
        "--reason",
        "cli handoff",
        "bootstrap",
        "submit",
        "--file",
        str(path),
        "--owner",
        OWNER,
        capsys=capsys,
    )
    assert from_cli["state"] == "verified" and from_cli["imported_by"] == "crucible-admin"
    via_api = admin_client.get(f"/v1/import/bootstrap/{from_cli['import_id']}").json()
    assert via_api == from_cli
    shown = run_cli(config_file, "bootstrap", "show", from_cli["import_id"], capsys=capsys)
    assert shown == via_api
    listed = run_cli(config_file, "bootstrap", "list", capsys=capsys)["items"]
    assert listed == admin_client.get("/v1/import/bootstrap").json()["items"]
    committed = run_cli(
        config_file,
        "--reason",
        "cli commit",
        "bootstrap",
        "commit",
        from_cli["import_id"],
        capsys=capsys,
    )
    assert committed["state"] == "authoritative" and committed["committed_by"] == "crucible-admin"
    assert admin_client.get(f"/v1/import/bootstrap/{from_cli['import_id']}").json() == committed
    # The same document shape on both entry points, and both in one audit trail.
    api_report = submit(admin_client, producer_bundle()).json()
    assert set(api_report) == set(from_cli)
    kinds = [
        (e["kind"], e["principal"])
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    ]
    assert ("bootstrap_import_verified", "crucible-admin") in kinds
    assert ("bootstrap_import_committed", "crucible-admin") in kinds
    assert ("bootstrap_import_verified", "admin-principal") in kinds
    # A rejected bundle through the CLI names every problem and exits 1.
    broken = synthetic_bundle()
    broken["tasks"][0]["state"] = "missing"
    broken_path = bundle_file(tmp_path, broken, "broken.json")
    with pytest.raises(SystemExit):
        admin_main(
            [
                "--config",
                str(config_file),
                "--reason",
                "x",
                "bootstrap",
                "submit",
                "--file",
                str(broken_path),
            ]
        )
    err = capsys.readouterr().out
    assert "bootstrap-bundle-invalid" in err and "tasks[0].state" in err


def test_the_cli_remote_mode_builds_the_bootstrap_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    raw = synthetic_bundle()
    path = bundle_file(tmp_path, raw)
    base = ["--api-url", "http://127.0.0.1:1"]
    admin_main(
        [*base, "--reason", "r", "bootstrap", "submit", "--file", str(path), "--owner", OWNER]
    )
    admin_main([*base, "bootstrap", "show", "imp-1"])
    admin_main([*base, "bootstrap", "list"])
    admin_main([*base, "--reason", "r", "bootstrap", "commit", "imp-1"])
    assert calls == [
        ("POST", f"/v1/import/bootstrap?reason=r&owner={OWNER}", raw),
        ("GET", "/v1/import/bootstrap/imp-1", None),
        ("GET", "/v1/import/bootstrap", None),
        ("POST", "/v1/import/bootstrap/imp-1/commit", {"reason": "r"}),
    ]


# ----- the reconstruction: Foundry's start-of-session from the API alone ----------------


def test_a_scripted_start_of_session_reconstructs_the_live_task_set(
    admin_client: TestClient, client: TestClient
) -> None:
    """15 step 8: after the handoff Foundry runs `GET /v1/tasks?state=...` and
    `GET /v1/wakes` and nothing else. The live set it gets is the live set the bundle
    implies (every task not in the producer's closed states), with the same external
    ids, and each task's events tell its story."""
    raw = synthetic_bundle()
    report = submit(admin_client, raw).json()
    admin_client.post(f"/v1/import/bootstrap/{report['import_id']}/commit", json={"reason": "go"})

    expected_live = {
        t["id"]: STATE_MAP[t["state"]].value
        for t in raw["tasks"]
        if t["state"] not in SOURCE_CLOSED_STATES
    }
    live_states = sorted({s for s in expected_live.values()})
    seen: dict[str, str] = {}
    for state in live_states:
        page = client.get("/v1/tasks", params={"state": state, "limit": 200}).json()
        for item in page["items"]:
            seen[item["external_id"]] = item["state"]
    assert seen == expected_live
    closed = {t["id"] for t in raw["tasks"] if t["state"] in SOURCE_CLOSED_STATES}
    for state in ("closed", "cancelled"):
        for item in client.get("/v1/tasks", params={"state": state}).json()["items"]:
            assert item["external_id"] in closed
    wakes = client.get("/v1/wakes").json()
    assert wakes["items"] == []
    for external_id in expected_live:
        (item,) = client.get("/v1/tasks", params={"external_id": external_id}).json()["items"]
        events = client.get(f"/v1/tasks/{item['id']}/events", params={"limit": 200}).json()["items"]
        kinds = [e["kind"] for e in events]
        assert kinds[0] in ("bootstrap_task_imported", "execution_created")
        assert kinds[-1] == "bootstrap_handoff"
        story = [e["payload"]["event"] for e in events if e["kind"] == "bootstrap_event_imported"]
        assert story == [e["event"] for e in raw["events"] if e["task"] == external_id]


def test_events_keep_their_order_and_original_seq_across_tasks(admin_client: TestClient) -> None:
    tasks = [bootstrap_task("SYN-0001", "done"), bootstrap_task("SYN-0002", "proposed")]
    events = [
        bootstrap_event(3, "SYN-0001", "proposed"),
        bootstrap_event(7, "SYN-0002", "proposed", ts="2026-01-05 10:00 CST"),
        bootstrap_event(9, "SYN-0001", "done", who="foundry", detail=None),
    ]
    from tests.fixtures import bootstrap_bundle  # noqa: PLC0415

    report = submit(admin_client, bootstrap_bundle(tasks, events)).json()
    rows = admin_client.get(
        "/v1/events", params={"kind": "bootstrap_event_imported", "limit": 200}
    ).json()["items"]
    assert [r["payload"]["seq"] for r in rows] == [3, 7, 9]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert rows[1]["ts"] == "2026-01-05T16:00:00.000000+00:00"
    assert rows[2]["payload"]["detail"] is None and rows[2]["payload"]["who"] == "foundry"
    assert report["events"] == {"first_seq": rows[0]["seq"], "last_seq": rows[2]["seq"]}
