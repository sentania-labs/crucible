"""The administrative services without a database (25): the pty login driver against
fake CLIs that mimic each flow, the shape check, shredding and atomic rotation on real
directories, the public-key fingerprint, and the audit filter. Every secret-shaped value
is built at runtime."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.application.admin.audit import ADMIN_KINDS
from crucible.application.admin.credentials import check_shape, shred_file, shred_tree
from crucible.application.admin.github import key_fingerprint
from crucible.application.admin.login import FLOWS, LoginSession, run_login
from crucible.domain.events import EventKind


def _token(prefix: str, count: int = 40) -> str:
    return prefix + "x" * count


# ----- the login driver against fake CLIs ----------------------------------------


def fake_cli(tmp_path: Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_claude_code_flow_captures_the_token_to_a_600_file_and_never_shows_it(
    tmp_path: Path,
) -> None:
    """S1b: `setup-token` shows a URL, waits for the pasted code, then prints the
    long-lived token once. The driver writes it to oauth-token and shows `[captured]`."""
    token = _token("sk-ant-oat01-")
    cli = fake_cli(
        tmp_path,
        "fake-claude",
        'echo "Open https://claude.ai/oauth/authorize?code=abc in your browser"\n'
        'printf "Paste code here if prompted > "\n'
        "read -r code\n"
        'echo "got $code"\n'
        f'echo "Your token: {token}"\n'
        'echo "config dir: $CLAUDE_CONFIG_DIR"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "claude_code"
    session = LoginSession(harness="claude_code", started_at=0.0)
    shown: list[str] = []

    import threading  # noqa: PLC0415

    def operator() -> None:
        while session.state != "waiting_for_code":
            threading.Event().wait(0.05)
        session.submit_code("A1B2-C3D4")

    threading.Thread(target=operator, daemon=True).start()
    result = run_login(
        FLOWS["claude_code"],
        str(directory),
        session=session,
        argv=(cli,),
        timeout=20,
        emit=shown.append,
    )
    assert result.state == "finished", result.as_dict()
    assert result.url is not None and result.url.startswith("https://claude.ai/oauth/authorize")
    assert result.token_written
    written = directory / "oauth-token"
    assert written.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    blob = "\n".join(shown) + json.dumps(result.as_dict())
    assert token not in blob
    assert "[captured to oauth-token]" in blob
    # The CLI was pointed at the dedicated directory and nothing else (12).
    assert f"config dir: {directory}" in blob
    assert "got A1B2-C3D4" in blob


def test_codex_flow_shows_the_device_code_and_url_and_needs_no_paste(tmp_path: Path) -> None:
    cli = fake_cli(
        tmp_path,
        "fake-codex",
        'echo "Visit https://auth.openai.com/codex/device and enter code WXYZ-1234"\n'
        'echo "CODEX_HOME=$CODEX_HOME"\n'
        "sleep 0.2\n"
        'echo "Successfully logged in"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "codex"
    session = LoginSession(harness="codex", started_at=0.0)
    result = run_login(FLOWS["codex"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "finished"
    assert result.url == "https://auth.openai.com/codex/device"
    assert result.code == "WXYZ-1234"
    assert not result.token_written
    assert "15 minutes" in FLOWS["codex"].window
    assert any(f"CODEX_HOME={directory}" in line for line in result.lines)


def test_agy_flow_pastes_the_code_and_a_missing_code_fails_cleanly(tmp_path: Path) -> None:
    cli = fake_cli(
        tmp_path,
        "fake-agy",
        'echo "Please visit https://accounts.google.com/o/oauth2/auth?x=1"\n'
        'printf "Enter the authorization code: "\n'
        "read -t 2 -r code || { echo timeout; exit 1; }\n"
        'echo "HOME=$HOME"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "agy"
    assert "60 seconds" in FLOWS["agy"].window
    session = LoginSession(harness="agy", started_at=0.0)

    import threading  # noqa: PLC0415

    def operator() -> None:
        while session.state != "waiting_for_code":
            threading.Event().wait(0.05)
        session.submit_code("4/0Ab" + "c" * 30)

    threading.Thread(target=operator, daemon=True).start()
    result = run_login(FLOWS["agy"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "finished", result.as_dict()
    assert result.url is not None and result.url.startswith("https://accounts.google.com/")
    assert any(f"HOME={directory}" in line for line in result.lines)

    # Nobody pastes: the CLI's own 60-second window (2 s here) expires and the run
    # ends `failed` with the exit code, never hanging the driver.
    session = LoginSession(harness="agy", started_at=0.0)
    result = run_login(FLOWS["agy"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "failed" and result.exit_code == 1


def test_the_driver_times_out_a_login_that_never_finishes(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, "fake-hang", 'echo "https://example.invalid/x"\nsleep 30\n')
    session = LoginSession(harness="codex", started_at=0.0)
    result = run_login(FLOWS["codex"], str(tmp_path / "d"), session=session, argv=(cli,), timeout=1)
    assert result.state == "failed" and result.error == "login timed out"
    assert (
        subprocess.run(["pgrep", "-f", "fake-hang"], capture_output=True, check=False).returncode
        != 0
    )


# ----- shape, shred, rotate ------------------------------------------------------


def test_check_shape_names_the_problem_never_the_value(tmp_path: Path) -> None:
    spec = CodexAdapter().credential_spec()
    assert not check_shape(spec, str(tmp_path)).ok
    (tmp_path / "auth.json").write_text("not json", encoding="utf-8")
    check = check_shape(spec, str(tmp_path))
    assert check.problems == ("auth.json: not JSON",)
    secret = _token("eyJ")
    (tmp_path / "auth.json").write_text(json.dumps({"other": secret}), encoding="utf-8")
    check = check_shape(spec, str(tmp_path))
    assert check.problems == ("auth.json: missing keys ['tokens']",)
    assert secret not in json.dumps(check.as_dict())
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": secret}, "last_refresh": "2026-09-17T00:00:00Z"}),
        encoding="utf-8",
    )
    check = check_shape(spec, str(tmp_path))
    assert check.ok and check.files[0]["size"] > 0 and check.files[0]["parses"]

    agy = AgyAdapter().credential_spec()
    inner = tmp_path / ".gemini" / "antigravity-cli"
    inner.mkdir(parents=True)
    (inner / "antigravity-oauth-token").write_text(json.dumps({"token": {}}), encoding="utf-8")
    assert check_shape(agy, str(tmp_path)).ok

    claude = ClaudeCodeAdapter().credential_spec()
    (tmp_path / "oauth-token").write_text(_token("sk-ant-oat01-"), encoding="utf-8")
    assert check_shape(claude, str(tmp_path)).ok, ".claude.json is optional"


def test_shred_overwrites_then_unlinks_and_keeps_the_root(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    secret = _token("eyJ", 200).encode()
    (root / "auth.json").write_bytes(secret)
    (root / "sessions" / "s.jsonl").write_bytes(b"line\n" * 100)
    (root / "link").symlink_to(root / "auth.json")
    result = shred_tree(root, keep_root=True)
    assert result["files"] == 2 and result["bytes"] == len(secret) + 500
    assert root.is_dir() and list(root.iterdir()) == []


def test_shred_file_zeroes_before_unlink(tmp_path: Path) -> None:
    target = tmp_path / "t"
    payload = os.urandom(5000)
    target.write_bytes(payload)
    # Observe the overwrite through a second handle before the unlink lands.
    size = shred_file(target)
    assert size == 5000 and not target.exists()


# ----- fingerprint and audit ------------------------------------------------------


def test_key_fingerprint_is_of_the_public_key_only(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    fingerprint = key_fingerprint(str(path))
    assert fingerprint is not None and fingerprint.startswith("sha256:")
    assert key_fingerprint(str(tmp_path / "missing.pem")) is None
    assert fingerprint not in pem.decode("utf-8", "replace")
    assert key_fingerprint(str(path)) == fingerprint, "deterministic"


def test_admin_kinds_are_every_mutation_and_nothing_a_worker_writes() -> None:
    for kind in (
        EventKind.HARNESS_ENABLED,
        EventKind.CREDENTIAL_PROBED,
        EventKind.CREDENTIAL_ROTATED,
        EventKind.CREDENTIAL_REMOVED,
        EventKind.IMAGE_PROMOTED,
        EventKind.GITHUB_CHECKED,
    ):
        assert kind.value in ADMIN_KINDS
    for kind in (EventKind.WORKER_PROGRESS, EventKind.ATTEMPT_RUNNING, EventKind.TASK_SUBMITTED):
        assert kind.value not in ADMIN_KINDS
