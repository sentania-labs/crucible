"""Launch-spec construction per adapter (07, 18): the exact flags, the prompt on stdin
as a pointer, no secret anywhere in argv or env, and the credential spec each declares.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.registry import default_adapters, default_registry
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.harnesses import effective_mount_mode
from crucible.domain.entities import HarnessState
from crucible.domain.secrets import scan_text
from crucible.ports.harness import (
    CredentialSource,
    HarnessAdapter,
    HarnessGate,
    HarnessUnavailableError,
    LaunchContext,
    MountMode,
)
from tests.fixtures import FakeClock

POINTER = "Read /crucible/identity/IDENTITY.md and execute the task."


def context(**overrides: Any) -> LaunchContext:
    base: dict[str, Any] = {
        "attempt_id": "01ATTEMPT0000000000000000A",
        "model": "model-x",
        "effort": None,
        "timeout_seconds": 900,
        "identity_mount": "/crucible/identity",
        "report_mount": "/crucible/report",
        "repo_mount": "/crucible/repo",
        "credential_mounted": True,
    }
    base.update(overrides)
    return LaunchContext(**base)


def test_claude_code_launch_matches_07() -> None:
    launch = ClaudeCodeAdapter().build_launch(context())
    assert launch.argv == (
        "claude",
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt-file",
        "/crucible/identity/IDENTITY.md",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "model-x",
    )
    assert launch.stdin_text == POINTER and launch.stdin_files == ()
    assert launch.env == {"CLAUDE_CONFIG_DIR": "/home/worker/.claude"}
    # The one exception to file-only delivery (07): a name and a path, never a value.
    assert launch.env_from_files == {"CLAUDE_CODE_OAUTH_TOKEN": "/home/worker/.claude/oauth-token"}
    assert launch.transcript_path == "/crucible/report/transcript.jsonl"


def test_codex_launch_matches_07_with_s1_and_s6_flags() -> None:
    launch = CodexAdapter().build_launch(context(effort="low"))
    argv = launch.argv
    assert argv[:2] == ("codex", "exec")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv  # S2: the container is the boundary
    assert "--skip-git-repo-check" in argv  # S1: the checkout is not a trusted host uid's
    assert argv[argv.index("--disable") + 1] == "plugins"  # S6
    assert "check_for_update_on_startup=false" in argv  # S7, S11
    assert argv[argv.index("-o") + 1] == "/crucible/report/codex-last-message.md"
    assert argv[argv.index("--model") + 1] == "model-x"
    assert argv[argv.index("-C") + 1] == "/crucible/repo"
    assert 'model_reasoning_effort="low"' in argv
    # IDENTITY.md followed by the prompt on stdin (07).
    assert launch.stdin_files == ("/crucible/identity/IDENTITY.md",)
    assert launch.stdin_text == POINTER
    assert launch.env == {"CODEX_HOME": "/home/worker/.codex"}
    assert launch.env_from_files == {}
    # Codex never gets a sandbox flag (S2).
    assert "--sandbox" not in argv


def test_agy_launch_matches_07_and_stays_under_the_argv_ceiling() -> None:
    launch = AgyAdapter().build_launch(context(effort="low", timeout_seconds=1200))
    argv = launch.argv
    assert argv[:3] == ("agy", "-p", POINTER)
    assert argv[argv.index("--model") + 1] == "model-x"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--add-dir") + 1] == "/crucible/identity"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # The CLI's own print timeout defaults to five minutes; it follows the attempt's.
    assert argv[argv.index("--print-timeout") + 1] == "1200s"
    assert all(len(arg) < 1024 for arg in argv), "S3: argv carries only a short pointer"
    assert launch.stdin_text == "" and launch.stdin_files == ()
    assert launch.env == {} and launch.env_from_files == {}


def test_script_harness_is_a_plain_argv() -> None:
    launch = ScriptHarnessAdapter().build_launch(context(credential_mounted=False))
    assert launch.argv == ("crucible-script-harness",)
    assert launch.transcript_path is None and launch.stdin_text == ""
    assert ScriptHarnessAdapter().credential_spec() is None


@pytest.mark.parametrize("adapter", default_adapters(), ids=lambda a: a.name)
def test_nothing_secret_shaped_in_any_launch(adapter: HarnessAdapter) -> None:
    """12: argv and env carry names, paths and flags; the scanner finds nothing."""
    ctx = (
        context(
            model="gpt-oss:120b",
            credential_mounted=False,
            endpoint="local",
            endpoint_url="http://spark.example.internal:11434/v1",
        )
        if isinstance(adapter, HermesAdapter)
        else context()
    )
    launch = adapter.build_launch(ctx)
    blob = "\n".join([*launch.argv, *launch.env.values(), *launch.env_from_files.values()])
    assert scan_text(blob) is None
    for value in launch.env_from_files.values():
        assert value.startswith("/home/worker/"), "a path inside the mounted copy, never a value"


def test_without_a_credential_mounted_no_config_env_is_set() -> None:
    for adapter in (ClaudeCodeAdapter(), CodexAdapter()):
        launch = adapter.build_launch(context(credential_mounted=False))
        assert launch.env == {} and launch.env_from_files == {}


# ----- credential specs (12) --------------------------------------------------


def test_credential_specs_name_only_the_auth_files_s1_recorded() -> None:
    claude = ClaudeCodeAdapter().credential_spec()
    assert [f.name for f in claude.auth_files] == ["oauth-token", ".claude.json"]
    assert claude.minimum_mode is MountMode.RW_NARROW
    assert claude.config_dir_env == "CLAUDE_CONFIG_DIR"
    assert set(claude.templates) == {"settings.json"}
    # Neither file is written back: the long-lived token never refreshes (S1b) and the
    # state file is state.
    assert all(not f.sync_back for f in claude.auth_files)

    codex = CodexAdapter().credential_spec()
    assert [f.name for f in codex.auth_files] == ["auth.json"]
    assert codex.minimum_mode is MountMode.RW_NARROW
    assert codex.auth_files[0].issued_at == ("last_refresh",)
    assert codex.auth_files[0].json and codex.auth_files[0].sync_back
    assert set(codex.templates) == {"config.toml"}

    agy = AgyAdapter().credential_spec()
    assert [f.name for f in agy.auth_files] == ["antigravity-cli/antigravity-oauth-token"]
    assert agy.minimum_mode is MountMode.RW_NARROW
    assert agy.source_subdir == ".gemini" and agy.mount_target == "/home/worker/.gemini"
    assert agy.auth_files[0].issued_at == ("token", "expiry")


def test_configuration_may_raise_the_mount_mode_and_never_lower_it() -> None:
    """25 step 7."""
    ro_minimum = replace(AgyAdapter().credential_spec(), minimum_mode=MountMode.RO)
    assert effective_mount_mode(ro_minimum, None) is MountMode.RO
    assert effective_mount_mode(ro_minimum, CredentialSource("/x")) is MountMode.RO
    assert effective_mount_mode(ro_minimum, CredentialSource("/x", MountMode.RW_NARROW)) is (
        MountMode.RW_NARROW
    )
    codex = CodexAdapter().credential_spec()
    assert effective_mount_mode(codex, CredentialSource("/x", MountMode.RO)) is (
        MountMode.RW_NARROW
    )


# ----- the registry (07, 25) ----------------------------------------------------


def test_the_registry_knows_the_five_harnesses() -> None:
    registry = default_registry()
    assert registry.names() == ("claude_code", "codex", "agy", "hermes", "script-harness")
    assert registry.require("codex").supported_versions.text == ">=0.153.0,<0.154.0"
    assert registry.require("claude_code").supported_versions.text == ">=2.1.0,<2.2.0"
    assert registry.require("agy").supported_versions.text == ">=1.2.0,<1.3.0"
    assert registry.require("hermes").supported_versions.text == ">=0.19.0,<0.20.0"


def test_an_unknown_name_is_refused() -> None:
    with pytest.raises(HarnessUnavailableError, match="no adapter declares"):
        default_registry().resolve("nonesuch")


def test_a_configuration_gate_refuses_with_its_reason() -> None:
    gates = {"codex": HarnessGate(enabled=False, reason="unverified (S1b)")}
    with pytest.raises(HarnessUnavailableError, match="disabled in configuration: unverified"):
        default_registry().resolve("codex", gates=gates)
    # Another harness is unaffected by a gate that does not name it.
    assert default_registry().resolve("claude_code", gates=gates).name == "claude_code"


def test_an_administrator_flag_refuses_with_its_reason() -> None:
    state = HarnessState(
        name="agy",
        enabled=False,
        reason="credential rotated; pending probe",
        session_compatibility="unverified",
        updated_at=FakeClock().now(),
        updated_by="admin",
    )
    with pytest.raises(HarnessUnavailableError, match="disabled by an administrator"):
        default_registry().resolve("agy", state=state)
    state.enabled = True
    assert default_registry().resolve("agy", state=state).name == "agy"
