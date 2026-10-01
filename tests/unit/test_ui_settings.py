"""Settings page rendering."""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.ui.pages import settings as ui_settings
from crucible.adapters.ui.pages.settings import _settings_rows
from crucible.settings import CredentialSettings, Settings


def test_settings_for_a_provider_that_is_off_are_not_listed() -> None:
    """crucible#125: a Kubernetes deployment does not list the Docker provider's
    settings, nor the credential directory settings its service-owned Secrets replace;
    each provider's own `enabled` row stays so the page still says it is off."""
    from crucible.settings import Settings  # noqa: PLC0415

    kubernetes = Settings(kubernetes={"enabled": True}, docker={"enabled": False})
    kubernetes.credentials = {"codex": CredentialSettings(path="/x")}
    paths = [row[0] for row in ui_settings._settings_rows(kubernetes)]
    assert "docker.enabled" in paths
    assert not [p for p in paths if p.startswith("docker.") and p != "docker.enabled"]
    assert any(p.startswith("kubernetes.") and p != "kubernetes.enabled" for p in paths)
    assert "credentials.codex.path" not in paths
    assert "credentials.codex.mount_mode" in paths
    docker = Settings(kubernetes={"enabled": False}, docker={"enabled": True})
    docker.credentials = {"codex": CredentialSettings(path="/x")}
    paths = [row[0] for row in ui_settings._settings_rows(docker)]
    assert "credentials.codex.path" in paths
    assert not [p for p in paths if p.startswith("kubernetes.") and p != "kubernetes.enabled"]


def test_once_a_harness_is_ready_the_others_gaps_leave_the_to_do_list() -> None:
    """Review of the first-run integration: Status must not read "ready" above a list of
    what stands before a task. Other harnesses' gaps stay under Details."""
    from crucible.adapters.ui.pages.dashboard import _readiness_sections  # noqa: PLC0415

    step = {"code": "credential_missing", "text": "codex has no credential.", "fix": "/ui/x"}
    codex = {"name": "codex", "state": "not_ready", "note": "", "steps": [step]}
    ready: dict[str, Any] = {
        "ready": True,
        "ready_harnesses": ["hermes"],
        "steps": [],
        "harnesses": [
            {"name": "hermes", "state": "ready", "note": "ready for a task", "steps": []},
            codex,
        ],
    }
    sections, (_summary, detail) = _readiness_sections(ready)
    assert sections == []
    assert ["codex", "not ready", step["text"], step["fix"]] in detail["rows"]
    none_ready = {
        **ready,
        "ready": False,
        "ready_harnesses": [],
        "harnesses": [codex],
    }
    sections, _ = _readiness_sections(none_ready)
    assert sections[0]["rows"] == [[step["text"], step["fix"]]]


def test_the_settings_page_shows_broad_egress_and_the_resolve_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 61: the two egress settings appear beside every other Kubernetes setting,
    with their source, and broad egress says what turning it on gives a worker."""
    monkeypatch.delenv("CRUCIBLE_CONFIG", raising=False)
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__RESOLVE_TTL_SECONDS", "120")
    rows = {
        # A provider's settings are listed only while it is on (crucible#125).
        row[0]: row
        for row in _settings_rows(
            Settings(kubernetes={"enabled": True, "resolve_ttl_seconds": 120})
        )
    }
    assert rows["kubernetes.broad_egress"][1:3] == [False, "default"]
    assert "GitHub included" in rows["kubernetes.broad_egress"][3]
    assert rows["kubernetes.resolve_ttl_seconds"][1:3] == [120.0, "environment"]
    # Every setting here is read at start, which the page intro says once (crucible#115).
    assert rows["kubernetes.resolve_ttl_seconds"][3] == ""
