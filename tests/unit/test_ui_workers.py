"""Workers page rendering."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.adapters.ui.pages import workers as ui_workers
from tests.unit.admin_ui_fixtures import request


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
    monkeypatch.setattr(ui_workers, "_require", lambda *_args: (principal, "fixture-csrf"))
    monkeypatch.setattr(
        ui_workers,
        "_page",
        lambda *_args, sections, **_kwargs: rendered.update(sections=sections),
    )

    ui_workers.worker_logs(
        request("/ui/workers/attempt-1/logs"),
        "attempt-1",
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(logs=logs)),
    )
    text = rendered["sections"][0]["text"]

    assert marker not in text
    assert "[redacted:github_token]" in text
