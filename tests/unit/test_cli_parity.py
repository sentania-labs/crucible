"""Parity: every verb of `crucible-admin` and of Foundry's `foundry-crucible` exists in
`crucible` with the same arguments and makes the same API calls.

The two old command trees are captured in tests/fixtures_data/legacy_cli/trees.json
(capture.py beside it says how), so this test holds the new command to them after the
old code is gone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from crucible.cli import admin as admin_cli
from crucible.cli.main import build_parser, run
from crucible.client.config import ADMIN_TOKEN_ENV
from crucible.client.http import Api
from tests.fixtures_data.legacy_cli.capture import walk

TREES = json.loads(
    (Path(__file__).parents[1] / "fixtures_data" / "legacy_cli" / "trees.json").read_text()
)
NEW = walk(build_parser())

# Where each old command lives now. crucible-admin's tree moved under `admin`;
# foundry-crucible's verbs are top-level verbs of `crucible`, unchanged.
MAPPED = [
    ("crucible-admin", path, " ".join(["admin", path]).strip())
    for path in TREES["crucible-admin"]["tree"]
] + [("foundry-crucible", path, path) for path in TREES["foundry-crucible"]["tree"]]


@pytest.mark.parametrize(("tool", "old", "new"), MAPPED, ids=[f"{t}:{o}" for t, o, _ in MAPPED])
def test_every_old_command_maps_to_a_new_one_with_the_same_arguments(
    tool: str, old: str, new: str
) -> None:
    before = TREES[tool]["tree"][old]
    assert new in NEW, f"{tool} `{old}` has no `crucible {new}`"
    after = NEW[new]
    assert after["positionals"][: len(before["positionals"])] == before["positionals"]
    if old:
        # A subcommand's positionals are exactly the old ones; the root may gain groups.
        assert after["positionals"] == before["positionals"]
    for flag, shape in before["options"].items():
        assert flag in after["options"], f"`crucible {new}` lost {flag}"
        assert after["options"][flag] == shape, f"`crucible {new}` {flag} changed"


def test_the_trees_were_captured_from_the_sources_the_contract_names() -> None:
    assert TREES["crucible-admin"]["ref"] == "c7cd915"
    assert len(TREES["crucible-admin"]["tree"]) == 49
    assert len(TREES["foundry-crucible"]["tree"]) == 18


# ----- the admin group makes the same remote calls ------------------------------------

ADMIN_CALLS: list[tuple[list[str], tuple[str, str, Any]]] = [
    (["status"], ("GET", "/v1/admin/status", None)),
    (
        ["task", "republish", "T1", "--reason", "back"],
        ("POST", "/v1/tasks/T1/republish", {"reason": "back"}),
    ),
    (["token", "list"], ("GET", "/v1/admin/tokens", None)),
    (
        ["--reason", "r", "token", "create", "--principal", "p", "--role", "observer"],
        ("POST", "/v1/admin/tokens", {"reason": "r", "name": "p", "role": "observer"}),
    ),
    (
        ["--reason", "r", "token", "revoke", "P1"],
        ("POST", "/v1/admin/tokens/P1/revoke", {"reason": "r"}),
    ),
    (["repositories", "list"], ("GET", "/v1/admin/repositories", None)),
    (["repository", "list"], ("GET", "/v1/admin/repositories", None)),
    (
        ["--reason", "r", "repository", "register", "--name", "n", "--url", "https://x/y"],
        (
            "PUT",
            "/v1/admin/repositories/n",
            {
                "reason": "r",
                "url": "https://x/y",
                "default_branch": "main",
                "policy_name": "default-software",
                "installation_id": None,
                "attested_all_prs": False,
                "attested_by": None,
            },
        ),
    ),
    (
        ["--reason", "r", "repositories", "remove", "n"],
        ("DELETE", "/v1/admin/repositories/n", {"reason": "r"}),
    ),
    (
        ["--reason", "r", "repository", "remove", "n"],
        ("DELETE", "/v1/admin/repositories/n", {"reason": "r"}),
    ),
    (["harnesses", "list"], ("GET", "/v1/admin/harnesses", None)),
    (
        ["--reason", "r", "harnesses", "enable", "codex"],
        ("POST", "/v1/admin/harnesses/codex/enable", {"reason": "r"}),
    ),
    (
        ["--reason", "r", "harnesses", "disable", "agy"],
        ("POST", "/v1/admin/harnesses/agy/disable", {"reason": "r"}),
    ),
    (["credentials", "status", "--harness", "codex"], ("GET", "/v1/admin/credentials/codex", None)),
    (
        ["--reason", "r", "credentials", "validate", "--harness", "codex"],
        ("POST", "/v1/admin/credentials/codex/validate", {"reason": "r"}),
    ),
    (
        ["--reason", "r", "credentials", "probe", "--harness", "codex"],
        ("POST", "/v1/admin/credentials/codex/probe", {"reason": "r"}),
    ),
    (
        ["--reason", "r", "credentials", "remove", "--harness", "codex"],
        ("POST", "/v1/admin/credentials/codex/remove", {"reason": "r"}),
    ),
    (
        ["--reason", "r", "credentials", "rotate", "--harness", "agy", "--new-path", "/p"],
        ("POST", "/v1/admin/credentials/agy/rotate", {"reason": "r", "new_path": "/p"}),
    ),
    (
        ["--reason", "r", "credentials", "set", "--harness", "hermes"],
        ("POST", "/v1/admin/credentials/hermes/set", {"reason": "r", "api_key": "vk_test_key"}),
    ),
    (["images", "list"], ("GET", "/v1/admin/images", None)),
    (
        # Promotion is per harness (ADR 0016): the harness is named.
        ["--reason", "r", "images", "promote", "sha256:abc", "--harness", "hermes"],
        ("POST", "/v1/admin/images/sha256:abc/promote", {"reason": "r", "harness": "hermes"}),
    ),
    (
        ["images", "rollback", "--harness", "agy"],
        ("POST", "/v1/admin/images/rollback", {"reason": "", "harness": "agy"}),
    ),
    (["providers", "status"], ("GET", "/v1/admin/providers", None)),
    (["github", "status"], ("GET", "/v1/admin/github", None)),
    (["--reason", "r", "github", "check"], ("POST", "/v1/admin/github/check", {"reason": "r"})),
    (["audit", "tail"], ("GET", "/v1/admin/audit?limit=50", None)),
    (
        ["audit", "tail", "--cursor", "5", "--limit", "10"],
        ("GET", "/v1/admin/audit?limit=10&cursor=5", None),
    ),
    (["routing", "exhaustion"], ("GET", "/v1/admin/routing/exhaustion", None)),
    (
        ["--reason", "r", "routing", "clear-exhaustion", "pool-a"],
        ("POST", "/v1/admin/routing/exhaustion/pool-a/clear", {"reason": "r"}),
    ),
    (["routing", "local-endpoint"], ("GET", "/v1/admin/routing/local-endpoint", None)),
    (
        [
            "--reason",
            "r",
            "routing",
            "set-local-endpoint",
            "--endpoint-url",
            "https://llm.example/v1",
            "--model",
            "coder",
            "--enable",
        ],
        (
            "POST",
            "/v1/admin/routing/local-endpoint",
            {
                "reason": "r",
                "endpoint_url": "https://llm.example/v1",
                "models": [{"id": "coder", "enabled": True, "enable_thinking": False}],
                "max_concurrency": 4,
            },
        ),
    ),
    (["bootstrap", "show", "I1"], ("GET", "/v1/import/bootstrap/I1", None)),
    (["bootstrap", "list"], ("GET", "/v1/import/bootstrap", None)),
    (
        ["--reason", "r", "bootstrap", "commit", "I1"],
        ("POST", "/v1/import/bootstrap/I1/commit", {"reason": "r"}),
    ),
]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Api, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setattr(admin_cli, "_read_api_key", lambda: "vk_test_key")
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    return calls


@pytest.mark.parametrize(("argv", "call"), ADMIN_CALLS, ids=[" ".join(a) for a, _ in ADMIN_CALLS])
def test_the_admin_group_makes_the_same_remote_calls(
    recorded: list[tuple[str, str, Any]],
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    call: tuple[str, str, Any],
) -> None:
    assert run(["admin", "--api-url", "http://127.0.0.1:1", *argv]) == 0
    assert recorded == [call]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_bootstrap_submit_sends_the_bundle_with_reason_and_owner(
    recorded: list[tuple[str, str, Any]], tmp_path: Path
) -> None:
    bundle = tmp_path / "crucible.json"
    bundle.write_text('{"schema": "bundle"}', encoding="utf-8")
    argv = ["--reason", "r", "bootstrap", "submit", "--file", str(bundle), "--owner", "foundry"]
    assert run(["admin", "--api-url", "http://127.0.0.1:1", *argv]) == 0
    assert recorded == [
        ("POST", "/v1/import/bootstrap?reason=r&owner=foundry", {"schema": "bundle"})
    ]


def test_every_admin_leaf_has_a_remote_call_or_is_named_here() -> None:
    """The table above covers the whole admin tree: nothing was left out of the check."""
    tree = TREES["crucible-admin"]["tree"]
    leaves = {path for path in tree if path and not any(p.startswith(path + " ") for p in tree)}
    checked = set()
    for argv, _ in ADMIN_CALLS:
        words = argv[2:] if argv[:1] == ["--reason"] else argv
        checked |= {" ".join(words[:size]) for size in (1, 2)} & leaves
    elsewhere = {
        "migrate": "CLI-only and local (25); test_remote_migrate_is_refused_as_usage",
        "credentials login": "interactive; tests/integration/test_admin.py drives it remotely",
        "bootstrap submit": "test_bootstrap_submit_sends_the_bundle_with_reason_and_owner",
        "repositories register": "the same parser and call as `repository register`",
    }
    assert leaves - checked == set(elsewhere), leaves - checked


def test_remote_migrate_is_refused_as_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["admin", "--api-url", "http://127.0.0.1:1", "migrate"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_the_legacy_admin_entry_point_warns_and_delegates(
    recorded: list[tuple[str, str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    from crucible.cli.main import admin_shim  # noqa: PLC0415

    with pytest.raises(SystemExit) as raised:
        admin_shim(["--api-url", "http://127.0.0.1:1", "status"])
    assert raised.value.code == 0
    captured = capsys.readouterr()
    assert captured.err.strip().splitlines() == [
        "crucible-admin is deprecated; use `crucible admin` (same arguments)"
    ]
    assert json.loads(captured.out)["kind"] == "admin_status"
    assert recorded == [("GET", "/v1/admin/status", None)]


def test_walk_sees_the_admin_group_under_the_new_root() -> None:
    parser = argparse.ArgumentParser()
    assert walk(parser) == {"": {"positionals": [], "options": {}}}
    assert "admin credentials rotate" in NEW
