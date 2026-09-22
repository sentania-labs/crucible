"""The generated scripts treat contract-derived values as data (08).

The commands a contract lists under `required_verification` are executed as given: that
is the point of the gate. Everything else a contract carries, refs, paths, check ids, is
bound to a shell variable from a single-quoted literal and referenced quoted, so it is
data to `sh` whatever it contains.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from crucible.adapters.execution import scripts, workspace
from crucible.ports.execution import IDENTITY_MOUNT, OUTPUT_MOUNT, REPORT_MOUNT

HOSTILE_REFS = [
    "crucible/$(touch /tmp/crucible-pwned)",
    "crucible/`touch /tmp/crucible-pwned`",
    "crucible/x; touch /tmp/crucible-pwned",
    "crucible/x' ; touch /tmp/crucible-pwned ; '",
    "crucible/x\ntouch /tmp/crucible-pwned",
    "--upload-pack=touch /tmp/crucible-pwned",
]


def preparer(work_branch: str, base_ref: str = "main") -> str:
    return scripts.preparer_script(
        url="/crucible/origin",
        base_ref=base_ref,
        work_branch=work_branch,
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
    )


def test_only_agents_md_is_a_generated_shim() -> None:
    script = preparer("crucible/test")
    assert "for shim in 'AGENTS.md'" in script
    assert "for shim in 'CLAUDE.md'" not in script


def test_a_project_claude_md_suppresses_the_agents_md_shim() -> None:
    script = preparer("crucible/test")
    assert 'if [ "$shim" = "AGENTS.md" ] && [ -e "$REPO/CLAUDE.md" ]' in script


def parses(script: str) -> None:
    """`sh -n` on the generated text: a quoting mistake is a syntax error or worse."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_a_hostile_ref_is_data_in_every_generated_script(ref: str) -> None:
    for script in (
        preparer(ref),
        preparer("crucible/x", ref),
        scripts.collector_script(base_ref=ref, work_branch=ref, size_cap_bytes=1024),
    ):
        parses(script)
        # The value appears only inside a single-quoted binding, never bare.
        for line in script.splitlines():
            if ref.splitlines()[0] in line:
                assert line.startswith(("WORK_BRANCH='", "BASE_REF='")), line


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_a_hostile_ref_does_not_execute(ref: str, tmp_path: Path) -> None:
    """Run the generated preparer under a stub git and assert the payload never ran."""
    marker = tmp_path / "pwned"
    stub = tmp_path / "bin"
    stub.mkdir()
    # A git that records its argv and always fails to find a ref, so the script takes
    # its error path with the hostile value in hand.
    (stub / "git").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$ARGV_LOG"\nexit 1\n', encoding="utf-8"
    )
    (stub / "git").chmod(0o755)
    (stub / "touch").write_text(
        f"#!/bin/sh\nprintf 'ran\\n' > {marker}\nexit 0\n", encoding="utf-8"
    )
    (stub / "touch").chmod(0o755)
    argv_log = tmp_path / "argv.log"
    script = tmp_path / "preparer.sh"
    script.write_text(preparer(ref), encoding="utf-8")
    subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{stub}:{shutil.which('sh') and '/usr/bin:/bin'}",
            "ARGV_LOG": str(argv_log),
            "HOME": str(tmp_path),
        },
    )
    assert not marker.exists(), "the ref executed"
    if argv_log.exists():
        # Whatever git was handed, it was one argument, not a command.
        assert "touch /tmp/crucible-pwned" not in argv_log.read_text().replace(ref, "")


def test_quota_checkpoint_ignores_worker_filter_and_signing_programs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    output = tmp_path / "output"
    report = tmp_path / "report"
    repo.mkdir()
    output.mkdir()
    report.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    filter_sentinel = tmp_path / "filter-ran"
    signing_sentinel = tmp_path / "signing-ran"
    subprocess.run(
        [
            "git",
            "config",
            "filter.evil.clean",
            f"sh -c 'touch {filter_sentinel}; cat'",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "gpg.program", f"sh -c 'touch {signing_sentinel}; exit 1'"],
        cwd=repo,
        check=True,
    )
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        quota_attempt_id="attempt-1",
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not filter_sentinel.exists()
    assert not signing_sentinel.exists()
    assert (
        subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .startswith("wip(crucible): attempt attempt-1")
    )


def test_quota_checkpoint_refuses_a_worker_commondir_redirect(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    common = tmp_path / "worker-common"
    output = tmp_path / "output"
    report = tmp_path / "report"
    for path in (repo, common, output, report):
        path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=common, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    filter_sentinel = tmp_path / "redirect-filter-ran"
    hook_sentinel = tmp_path / "redirect-hook-ran"
    hooks = common / ".git" / "hooks-redirected"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {hook_sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=common, check=True)
    subprocess.run(
        [
            "git",
            "config",
            "filter.evil.clean",
            f"sh -c 'touch {filter_sentinel}; cat'",
        ],
        cwd=common,
        check=True,
    )
    (repo / ".git" / "commondir").write_text(str(common / ".git") + "\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        quota_attempt_id="attempt-redirect",
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 4
    assert "commondir redirect" in result.stderr
    assert "commondir redirect" in (output / "checkpoint-refusal.txt").read_text()
    assert not filter_sentinel.exists()
    assert not hook_sentinel.exists()


def test_a_verification_id_cannot_collide_with_another() -> None:
    """`a/b` and `a_b` are two ids, so they get two files and two exit codes."""
    assert scripts.encode_check_id("a/b") == "a%2Fb"
    assert scripts.encode_check_id("a_b") == "a_b"
    assert scripts.encode_check_id("a/b") != scripts.encode_check_id("a_b")
    assert scripts.encode_check_id("..") not in ("", ".", "..")
    assert "/" not in scripts.encode_check_id("../../etc/passwd")


def test_the_verifier_writes_one_file_per_id(tmp_path: Path) -> None:
    """Two ids that a plain substitution would collapse produce distinct exits."""
    checks = [("a/b", "exit 3"), ("a_b", "exit 4"), ("plain", "exit 0")]
    script = scripts.verifier_script(checks)
    parses(script)
    verify = tmp_path / "verify"
    repo = tmp_path / "repo"
    verify.mkdir()
    repo.mkdir()
    body = script.replace(scripts.VERIFY_MOUNT, str(verify)).replace(scripts.REPO_MOUNT, str(repo))
    subprocess.run(["sh", "-c", body], check=True, capture_output=True)
    exits = {path.stem: path.read_text().strip() for path in verify.glob("*.exit")}
    assert exits == {"a%2Fb": "3", "a_b": "4", "plain": "0"}
    manifest = dict(
        line.split("\t", 1)
        for line in (verify / scripts.MANIFEST).read_text().splitlines()
        if "\t" in line
    )
    assert manifest == {"a%2Fb": "a/b", "a_b": "a_b", "plain": "plain"}


def test_a_verification_id_that_looks_like_a_command_stays_a_file_name(tmp_path: Path) -> None:
    script = scripts.verifier_script([("x'; touch /tmp/crucible-pwned; '", "exit 7")])
    parses(script)
    assert "touch /tmp/crucible-pwned" in script  # inside the quoted id, as data
    verify = tmp_path / "verify"
    repo = tmp_path / "repo"
    verify.mkdir()
    repo.mkdir()
    body = script.replace(scripts.VERIFY_MOUNT, str(verify)).replace(scripts.REPO_MOUNT, str(repo))
    subprocess.run(["sh", "-c", body], check=True, capture_output=True)
    assert not Path("/tmp/crucible-pwned").exists()
    (exit_file,) = list(verify.glob("*.exit"))
    assert exit_file.read_text().strip() == "7"
