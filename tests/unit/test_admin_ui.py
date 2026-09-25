"""Small template checks for the script-free administrative surface."""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence.migrations.versions._0001_walking_skeleton import (
    DEFAULT_POLICY,
)
from crucible.adapters.persistence.migrations.versions._0008_harness_adapters import (
    VERIFIED_ROUTING,
)
from crucible.adapters.ui import router as ui_router
from crucible.adapters.ui.router import (
    _document_section,
    _localize,
    _panel,
    _readiness_gaps,
    _safe_value,
    templates,
)
from crucible.application.admin import audit as audit_service
from crucible.application.admin import bootstrap as bootstrap_service
from crucible.application.admin import github as github_service
from crucible.application.admin import providers as providers_service
from crucible.application.admin import routing as routing_service
from crucible.application.admin import status as status_service
from crucible.application.queries import supervisor_view
from crucible.domain.entities import (
    BootstrapImport,
    Event,
    Lease,
    PoolExhaustion,
    RetentionAction,
    SupervisorStatus,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState


def request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def base_context(path: str) -> dict[str, object]:
    return {
        "request": request(path),
        "title": "Test",
        "active": "/ui",
        "nav": (("/ui", "Status"),),
        "principal": SimpleNamespace(name="reader", role=SimpleNamespace(value="observer")),
        "csrf": "fixture-csrf",
        "message": None,
    }


def test_page_template_uses_lattice_in_order_and_carries_csrf() -> None:
    rendered = templates.get_template("page.html").render(
        **base_context("/ui"),
        heading="Status",
        intro="Fixture",
        sections=[
            {
                "title": "Action",
                "form": {
                    "action": "/ui/actions/harness",
                    "label": "Apply",
                    "fields": [{"name": "reason", "label": "Reason", "required": True}],
                },
            }
        ],
        badge=None,
    )
    assert rendered.index("/ui/static/tokens.css") < rendered.index("/ui/static/lattice.css")
    assert 'data-theme="dark"' in rendered
    assert 'name="csrf" value="fixture-csrf"' in rendered
    assert "<script" not in rendered


def test_login_template_has_refresh_fallback_and_only_polling_script() -> None:
    context = base_context("/ui/credentials/codex/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    rendered = templates.get_template("login.html").render(
        **context,
        harness="codex",
        login={
            "state": "starting",
            "window": "Device flow",
            "url": None,
            "code": None,
            "output_tail": [],
            "error": None,
        },
    )
    assert "Refresh status" in rendered
    assert rendered.count("<script>") == 1
    assert "setTimeout" in rendered


def test_a_finishing_login_says_it_is_cleaning_up_and_offers_no_cancel() -> None:
    context = base_context("/ui/credentials/codex/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    rendered = templates.get_template("login.html").render(
        **context,
        harness="codex",
        login={
            "state": "finishing",
            "window": "Device flow",
            "url": None,
            "code": None,
            "output_tail": [],
            "error": None,
        },
    )
    assert "nothing left to cancel" in rendered
    assert "/ui/actions/login-cancel" not in rendered
    assert "/ui/actions/login-code" not in rendered
    assert "/ui/actions/login-finish" not in rendered
    assert "setTimeout" in rendered


def test_reader_login_page_has_state_but_no_operator_controls() -> None:
    rendered = templates.get_template("login.html").render(
        **base_context("/ui/credentials/codex/login"),
        harness="codex",
        login={
            "state": "waiting_for_operator",
            "window": "Device flow",
            "url": "https://example.invalid/device",
            "code": "ABCD-EFGH",
            "output_tail": ["sensitive operator output"],
            "error": None,
        },
    )
    assert "Administrator access is required" in rendered
    assert "waiting_for_operator" in rendered
    assert "ABCD-EFGH" not in rendered
    assert "sensitive operator output" not in rendered
    assert "/ui/actions/login-" not in rendered
    assert "<script>" not in rendered


def _render_documents(sections: list[dict[str, Any]]) -> str:
    return templates.get_template("page.html").render(
        **base_context("/ui"),
        heading="Readable panels",
        intro="Fixture",
        sections=_localize(sections, "America/Chicago"),
        badge=None,
    )


def _panel_leaves(panel: dict[str, Any]) -> list[Any]:
    if panel["kind"] == "fields":
        leaves: list[Any] = []
        for item in panel["items"]:
            leaves.extend(_panel_leaves(item["panel"]) if "panel" in item else [item["value"]])
        return leaves
    if panel["kind"] == "table":
        return [
            leaf
            for row in panel["rows"]
            for cell in row
            for leaf in (
                _panel_leaves(cell)
                if isinstance(cell, dict) and "kind" in cell
                else _document_leaves(cell)
            )
        ]
    if panel["kind"] == "values":
        return [leaf for item in panel["items"] for leaf in _document_leaves(item)]
    if panel["kind"] == "empty":
        return []
    return [panel["value"]]


def _document_leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _document_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _document_leaves(item)]
    return [value]


def test_readable_panel_preserves_every_supervisor_service_leaf() -> None:
    document = _supervisor_document(_lease(), _status())
    panel = _panel(document)
    rendered = _render_documents([_document_section("Supervisor", document)])

    assert len(_panel_leaves(panel)) == len(_document_leaves(document))
    assert "Lease holder" in rendered
    assert "Last successful tick" in rendered
    assert "Fenced token" in rendered
    assert "not displayed" not in rendered
    assert "healthy" in rendered
    assert "2026-09-21 01:30:00 AM CDT" in rendered
    assert "2026-09-21T06:30:00" not in rendered
    assert "<pre" not in rendered
    assert "{&#34;" not in rendered and '{"' not in rendered


def test_all_document_sections_suppress_secret_shaped_values() -> None:
    marker = "LEAK-MARKER-7f394b"
    document = {
        "token": marker,
        "access_token": marker,
        "refresh_token": marker,
        "device_code": marker,
        "password": marker,
        "webhook_secret": marker,
        "private_key": marker,
        "credential_value": marker,
        "authorization": marker,
        "device_url": f"https://example.invalid/device?user_code={marker}",
        "key_present": True,
        "webhook_secret_present": False,
    }
    titles = (
        "Supervisor",
        "Providers",
        "Status task state",
        "Pending wakes",
        "Active policy",
        "Routing policy",
        "Pool exhaustion",
        "App and repository connectivity",
        "Task state",
        "Wakes",
        "Summary",
        "Next cursor",
        "Imports",
        "Manifest",
    )
    rendered = _render_documents([_document_section(title, document) for title in titles])

    assert marker not in rendered
    assert "not displayed" in rendered
    assert "present" in rendered and "absent" in rendered


def test_harness_version_maps_are_shown_whatever_the_harness_is_called() -> None:
    """crucible#126: the audit of an image promotion lists every harness's version, and
    `claude_code` is a harness name, not a login code. Redaction goes by what a field
    holds, so a code, a token or a key under the same document stays hidden."""
    marker = "LEAK-MARKER-126"
    payload = {
        "reason": "",
        "harnesses": {
            "agy": "1.2.8",
            "claude_code": "2.1.280",
            "codex": "0.156.0",
            "hermes": "0.19.0",
            "script-harness": "1.0.0",
        },
        "code": marker,
        "user_code": marker,
        "login_token": marker,
        "error_code": 70,
        "api_key_set": True,
    }

    rendered = _render_documents([_document_section("Audit", payload)])

    for version in ("1.2.8", "2.1.280", "0.156.0", "0.19.0", "1.0.0"):
        assert version in rendered
    assert marker not in rendered
    assert rendered.count("not displayed") == 3
    assert "70" in rendered
    assert _safe_value("harnesses.claude_code", "2.1.280") == "2.1.280"
    assert _safe_value("claudeCode", "2.1.280") == "2.1.280"
    assert _safe_value("device_code", "ABCD-EFGH") == "not displayed"


def test_nested_lists_stay_readable_and_suppress_secrets_at_any_depth() -> None:
    marker = "ghp_" + "q" * 40
    document = {
        "entries": [
            {
                "description": f"provider returned {marker}",
                "details": [{"access_token": "LEAK-MARKER", "state": "ready"}],
                "checks": {},
                "lease": {"fenced_token": 27, "exit_code": 70},
            }
        ]
    }

    rendered = _render_documents([_document_section("Nested", document)])

    assert marker not in rendered
    assert "LEAK-MARKER" not in rendered
    assert "[redacted:github_token]" in rendered
    assert "not displayed" in rendered
    assert "ready" in rendered
    assert "Checks" in rendered and ">none<" in rendered
    assert "27" in rendered and "70" in rendered
    assert "{'" not in rendered and '{"' not in rendered


def test_coded_urls_and_manual_table_cells_are_sanitized() -> None:
    code = "ABCD-EFGH"
    marker = "ghp_" + "q" * 40
    sections = [
        {
            "title": "Repositories",
            "columns": ["URL", "Description"],
            "rows": [["https://user:password@example.invalid/repo", f"failure {marker}"]],
        }
    ]

    rendered = _render_documents(sections)

    assert "user:password" not in rendered
    assert marker not in rendered
    assert "[redacted:github_token]" in rendered
    assert code not in _safe_value("device_url", f"https://example.invalid/device#user_code={code}")
    assert code not in _safe_value(
        "device_url", f"https://example.invalid/device?user%5Fcode={code}"
    )
    assert _safe_value("repository_url", "https://[") == "invalid URL"


def test_invalid_timestamp_shaped_text_does_not_break_localization() -> None:
    detail = "provider failed near 2026-99-99T99:99:99Z"

    assert _localize(detail, "America/Chicago") == detail


def test_document_pages_have_no_generic_dump_markup() -> None:
    rendered = _render_documents(
        [
            _document_section("Fields", {"lease_holder": "supervisor-a", "healthy": True}),
            _document_section("Table", [{"attempt_id": "attempt-1", "active": False}]),
            _document_section("Empty", []),
        ]
    )

    filename = templates.get_template("page.html").filename
    assert filename is not None
    source = Path(filename).read_text(encoding="utf-8")
    assert "<pre" not in rendered
    assert "tojson" not in source
    assert "section.json" not in source
    assert "supervisor-a" in rendered and "attempt-1" in rendered
    assert "inactive" in rendered and ">none<" in rendered


def test_live_log_tail_is_the_only_page_template_preformatted_text() -> None:
    rendered = _render_documents([{"title": "Tail", "text": "worker output"}])

    assert rendered.count("<pre") == 1
    assert "worker output" in rendered


def test_live_log_tail_redacts_secret_shaped_output(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "ghp_" + "q" * 40
    logs = SimpleNamespace(
        last_offset=lambda _attempt_id: len(marker),
        list_from_offset=lambda _attempt_id, **_kwargs: [
            SimpleNamespace(content=f"before {marker} after".encode())
        ],
    )
    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    rendered: dict[str, Any] = {}
    monkeypatch.setattr(ui_router, "_require", lambda *_args: (principal, "fixture-csrf"))
    monkeypatch.setattr(
        ui_router,
        "_page",
        lambda *_args, sections, **_kwargs: rendered.update(sections=sections),
    )

    ui_router.worker_logs(
        request("/ui/workers/attempt-1/logs"),
        "attempt-1",
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(logs=logs)),
    )
    text = rendered["sections"][0]["text"]

    assert marker not in text
    assert "[redacted:github_token]" in text


NOW = datetime(2026, 9, 21, 6, 30, tzinfo=UTC)


def _supervisor_document(
    lease: Lease | None, supervisor_status: SupervisorStatus
) -> dict[str, Any]:
    uow = SimpleNamespace(
        leases=SimpleNamespace(get_supervisor=lambda: lease),
        supervisor_status=SimpleNamespace(get=lambda: supervisor_status),
        wakes=SimpleNamespace(count_unacked=lambda: 0),
        pull_requests=SimpleNamespace(list_in_states=lambda _states: []),
        github_deliveries=SimpleNamespace(count_unprocessed=lambda: 0),
    )
    return supervisor_view(uow, [], NOW, 30).model_dump(mode="json")


def _lease() -> Lease:
    return Lease(
        id="supervisor",
        kind="supervisor",
        key="singleton",
        holder="test-supervisor",
        fenced_token=1,
        expires_at=NOW + timedelta(seconds=30),
    )


def _status(**overrides: Any) -> SupervisorStatus:
    values: dict[str, Any] = {
        "holder": "test-supervisor",
        "last_tick_at": NOW,
        "tick_ms": 5,
        "counts": {},
        "last_success_at": NOW,
        "last_error_at": None,
        "last_error": None,
    }
    values.update(overrides)
    return SupervisorStatus(**values)


def test_healthy_supervisor_adds_no_readiness_gap() -> None:
    document = {"supervisor": _supervisor_document(_lease(), _status()), "harnesses": []}

    assert _readiness_gaps(document, repository_registered=True) == []


@pytest.mark.parametrize(
    ("lease", "supervisor_status", "cause"),
    [
        (None, _status(), "no supervisor lease"),
        (
            _lease(),
            _status(last_success_at=NOW - timedelta(seconds=31)),
            "no successful tick within the lease window",
        ),
        (
            _lease(),
            _status(
                last_success_at=NOW - timedelta(seconds=1),
                last_error_at=NOW,
                last_error="RuntimeError: test failure",
            ),
            "last tick failed: RuntimeError: test failure",
        ),
    ],
)
def test_each_unhealthy_supervisor_shape_adds_one_cause_specific_gap(
    lease: Lease | None, supervisor_status: SupervisorStatus, cause: str
) -> None:
    document = {
        "supervisor": _supervisor_document(lease, supervisor_status),
        "harnesses": [],
    }

    gaps = _readiness_gaps(document, repository_registered=True)

    assert len(gaps) == 1
    assert cause in gaps[0][0]


class _StrictStatusDict(dict[str, Any]):
    """Make optional access to an undefined status field fail like indexed access."""

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            raise AssertionError(f"gap logic read undefined status field {key!r}")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            raise AssertionError(f"gap logic read undefined status field {key!r}")
        return super().get(key, default)


def _strict_status(value: Any) -> Any:
    if isinstance(value, dict):
        return _StrictStatusDict({key: _strict_status(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_strict_status(item) for item in value]
    return value


@pytest.mark.asyncio
async def test_gap_logic_reads_only_keys_from_real_status_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_images(_ctx: Any) -> list[Any]:
        return []

    async def provider_status(_ctx: Any) -> dict[str, Any]:
        return {}

    harnesses = [
        {
            "name": "disabled",
            "enabled": False,
            "enabled_by_configuration": True,
            "credential": {"state": "absent"},
            "images": [],
        },
        {
            "name": "unpromoted",
            "enabled": True,
            "enabled_by_configuration": True,
            "credential": {"state": "valid"},
            "images": [{"promotion_state": "candidate"}],
        },
    ]
    supervisor = _supervisor_document(None, _status())
    status_module = cast(Any, status_service)
    monkeypatch.setattr(status_service, "list_images", list_images)
    monkeypatch.setattr(status_service, "list_harnesses", lambda _ctx, _uow, _images: harnesses)
    monkeypatch.setattr(status_service, "providers_status", provider_status)
    monkeypatch.setattr(
        status_service,
        "supervisor_view",
        lambda *_args: SimpleNamespace(model_dump=lambda **_kwargs: supervisor),
    )
    monkeypatch.setattr(status_module.github, "status", lambda _ctx, _uow: {})
    monkeypatch.setattr(status_service, "workers", lambda _uow: [])
    monkeypatch.setattr(status_service, "tasks", lambda _uow: {})
    monkeypatch.setattr(status_service, "wakes", lambda _uow: {})
    monkeypatch.setattr(status_service, "retention", lambda _uow: {})
    monkeypatch.setattr(status_module.bootstrap, "status_part", lambda _uow: {})
    monkeypatch.setattr(status_module.audit, "tail", lambda _uow, **_kwargs: {"next_cursor": None})
    document = await status_service.status(
        cast(
            Any,
            SimpleNamespace(
                providers={}, clock=SimpleNamespace(now=lambda: NOW), lease_ttl_seconds=30
            ),
        ),
        cast(Any, SimpleNamespace()),
    )

    gaps = _readiness_gaps(_strict_status(document), repository_registered=False)

    assert len(gaps) == 5


def test_all_fifteen_sections_preserve_real_service_output_shapes() -> None:
    blocked_task = SimpleNamespace(
        id="01BLOCKEDTASK00000000000000",
        external_id="FDY-READABLE",
        updated_at=NOW,
    )
    tasks_uow = SimpleNamespace(
        tasks=SimpleNamespace(
            list_by_state=lambda state: [blocked_task] if state is TaskState.BLOCKED else []
        )
    )
    wake = SimpleNamespace(created_at=NOW)
    wakes_uow = SimpleNamespace(
        principals=SimpleNamespace(
            list_all=lambda: [SimpleNamespace(id="principal-1", name="operator")]
        ),
        wakes=SimpleNamespace(
            list_for_principal=lambda *_args, **_kwargs: [wake],
            count_unacked=lambda: 1,
        ),
    )
    retention_action = RetentionAction(
        id="01RETENTION000000000000000",
        kind="logs_removed",
        subject="attempt-1",
        policy_name="default-software",
        policy_version=1,
        acted_at=NOW,
        detail={"bytes": 4096},
    )
    retention_uow = SimpleNamespace(
        retention=SimpleNamespace(list_recent=lambda _limit: [retention_action])
    )
    exhaustion = PoolExhaustion(
        pool="openai-sub",
        exhausted_at=NOW,
        reset_at=NOW + timedelta(hours=5),
        task_id="01BLOCKEDTASK00000000000000",
        attempt_id="01ATTEMPT0000000000000000",
        reason="soft limit reached",
    )
    routing_document = routing_service.list_exhaustions(
        cast(Any, SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW))),
        cast(
            Any,
            SimpleNamespace(pool_exhaustions=SimpleNamespace(list_all=lambda: [exhaustion])),
        ),
    )
    provider_document = asyncio.run(
        providers_service.providers_status(
            cast(Any, SimpleNamespace(providers={"fake": FakeProvider()}))
        )
    )
    github_document = github_service.status(
        cast(
            Any,
            SimpleNamespace(
                github=object(),
                github_app=SimpleNamespace(
                    app_id=1234,
                    api_base="https://api.github.com",
                    private_key_path=None,
                    webhook_secret_path=None,
                    webhook_enabled=True,
                ),
            ),
        ),
        cast(
            Any,
            SimpleNamespace(
                repositories=SimpleNamespace(
                    list_all=lambda: [
                        SimpleNamespace(name="sentania-labs/crucible", installation_id=55)
                    ]
                ),
                events=SimpleNamespace(list_global=lambda **_kwargs: []),
            ),
        ),
    )
    manifest = {
        "import_id": "01IMPORT000000000000000000",
        "state": "verified",
        "counts": {"tasks": 2, "events": 3},
        "principal": "operator",
        "verified_at": NOW.isoformat(),
        "committed_at": None,
        "tasks": [{"external_id": "FDY-1", "state": "closed"}],
    }
    import_record = BootstrapImport(
        id=str(manifest["import_id"]),
        state="verified",
        schema_version="1.0",
        content_sha256="a" * 64,
        source_sha256="b" * 64,
        source={"kind": "foundry-ledger"},
        manifest=manifest,
        principal_id="principal-1",
        imported_by="admin",
        verified_at=NOW,
    )
    bootstrap_uow = cast(
        Any,
        SimpleNamespace(
            bootstrap_imports=SimpleNamespace(
                list_all=lambda: [import_record],
                get=lambda import_id: import_record if import_id == import_record.id else None,
            )
        ),
    )
    imports_document = bootstrap_service.list_imports(bootstrap_uow)
    manifest_document = bootstrap_service.show(bootstrap_uow, import_record.id)
    audit_event = Event(
        seq=27,
        ts=NOW,
        kind=EventKind.HARNESS_ENABLED.value,
        principal="admin",
        verified=True,
        payload={"harness": "codex", "reason": "operator enabled"},
    )

    def audit_rows(*, after_seq: int, **_kwargs: Any) -> list[Event]:
        return [audit_event] if after_seq < (audit_event.seq or 0) else []

    audit_document = audit_service.tail(
        cast(Any, SimpleNamespace(events=SimpleNamespace(list_global=audit_rows))),
        cursor=0,
        limit=100,
    )
    task_document = status_service.tasks(cast(Any, tasks_uow))
    wake_document = status_service.wakes(cast(Any, wakes_uow))
    retention_document = status_service.retention(cast(Any, retention_uow))
    documents: list[tuple[str, Any | None]] = [
        ("Supervisor", _supervisor_document(_lease(), _status())),
        ("Providers", provider_document),
        ("Status task state", task_document),
        ("Pending wakes", wake_document),
        ("Active policy", DEFAULT_POLICY),
        ("Routing policy", VERIFIED_ROUTING),
        ("Pool exhaustion", routing_document),
        ("App and repository connectivity", github_document),
        ("Task state", task_document),
        ("Wakes", wake_document),
        ("Summary", retention_document),
        ("Next cursor", {"next_cursor": audit_document["next_cursor"]}),
        ("Imports", imports_document),
        ("Tail", None),
        ("Manifest", manifest_document),
    ]

    assert len(documents) == 15
    for title, document in documents:
        if document is None:
            rendered = _render_documents([{"title": title, "text": "worker output"}])
            assert "worker output" in rendered
            continue
        section = _document_section(title, document)
        panel = cast(dict[str, Any], _localize(section["panel"], "America/Chicago"))
        rendered = html.unescape(_render_documents([section]))
        assert len(_panel_leaves(panel)) == len(_document_leaves(document)), title
        for leaf in _panel_leaves(panel):
            values = leaf if isinstance(leaf, list) else [leaf]
            for value in values:
                assert str(value) in rendered, (title, value)


def test_a_reason_is_asked_for_only_where_the_service_requires_one() -> None:
    """crucible#117: one rule sets every form's reason field. Required on the
    destructive forms, optional on the rest, and absent on a read-only check."""

    def form(action: str) -> dict[str, Any]:
        return {
            "title": action,
            "form": {
                "action": action,
                "fields": [
                    {"name": "harness", "label": "Harness"},
                    {"name": "reason", "label": "Reason", "required": True},
                ],
            },
        }

    sections = ui_router._reason_fields(
        [
            form("/ui/actions/token-revoke"),
            form("/ui/actions/harness"),
            form("/ui/actions/github-check"),
        ]
    )
    reasons = [
        [f for f in section["form"]["fields"] if f["name"] == "reason"] for section in sections
    ]
    assert reasons[0] == [{"name": "reason", "label": "Reason", "required": True}]
    assert reasons[1] == [{"name": "reason", "label": "Reason (optional)", "required": False}]
    assert reasons[2] == []
