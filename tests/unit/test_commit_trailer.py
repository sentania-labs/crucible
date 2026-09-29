"""Crucible puts the attempt trailer on the worker's commits, and the collector runs the
publisher's commit check so a bad commit fails before review (hades FDY-0135).

These run real git against a scratch repository: the hook as the identity bundle writes
it, and the collector script as the collector container runs it."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from crucible.adapters.execution import identity, kubernetes, scripts
from crucible.adapters.execution.collected import read_commit_policy
from crucible.contracts.policy import Gates
from crucible.ports.execution import IDENTITY_MOUNT, OUTPUT_MOUNT, REPORT_MOUNT
from tests.fixtures import contract_document

AUTHOR = "crucible-worker@users.noreply.github.com"
POLICY = {"git": {"author_name": "crucible-worker", "author_email": AUTHOR}}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout


def _bundle(tmp_path: Path, external_id: str = "HT-0007") -> Path:
    directory = tmp_path / "identity"
    identity.write_bundle(
        directory,
        contract=contract_document(),
        policy=POLICY,
        external_id=external_id,
        owner="foundry",
        work_branch="crucible/test",
        network_mode="none",
        report_schema={},
    )
    return directory


def _checkout(tmp_path: Path, hooks: Path | None) -> Path:
    """A checkout configured the way the preparer leaves it: the policy's author, and
    core.hooksPath at the identity bundle's hook directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "crucible-worker")
    _git(repo, "config", "user.email", AUTHOR)
    if hooks is not None:
        _git(repo, "config", "core.hooksPath", str(hooks))
    return repo


def _commit(repo: Path, name: str, *messages: str, env: dict[str, str] | None = None) -> None:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name, env=env)
    args = [part for message in messages for part in ("-m", message)]
    _git(repo, "commit", "-q", *args, env=env)


def _trailers(repo: Path, rev: str = "HEAD") -> list[str]:
    out = _git(repo, "show", "-s", "--format=%(trailers:key=Crucible-Attempt,valueonly)", rev)
    return [line for line in out.splitlines() if line.strip()]


def _git_only_path(tmp_path: Path) -> dict[str, str]:
    """An environment whose PATH holds git and nothing else: the hook can reach no curl,
    no ssh and no other program, so whatever it does, it does with git alone."""
    bin_dir = tmp_path / "git-only"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (bin_dir / "git").symlink_to(git)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = str(bin_dir)
    # Nothing it could use to reach a network either way.
    env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
    return env


def test_the_bundle_carries_an_executable_read_only_commit_msg_hook(tmp_path: Path) -> None:
    hook = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR / "commit-msg"
    assert hook.stat().st_mode & 0o777 == 0o555
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "KEY='Crucible-Attempt'" in text
    assert "VALUE='HT-0007'" in text
    # The only program it runs is git's own trailer editor, on the message file.
    commands = [
        line
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" not in line.split(" ")[0]
    ]
    assert [c for c in commands if "git" in c] == [
        "exec git interpret-trailers --in-place --no-divider --if-exists doNothing \\"
    ]
    assert '  --if-missing add --trailer "$KEY: $VALUE" "$1"' in text


def test_the_hook_value_is_data_whatever_the_external_id_holds(tmp_path: Path) -> None:
    hostile = "HT-1'; touch /tmp/crucible-pwned; '"
    text = (_bundle(tmp_path, hostile) / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert scripts._quote(hostile) in text


def test_the_hook_adds_the_trailer_once(tmp_path: Path) -> None:
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    env = _git_only_path(tmp_path)
    _commit(repo, "greet.sh", "Add greet.sh, tests/test_greet.sh, and Makefile", env=env)
    assert _trailers(repo) == ["HT-0007"]
    message = _git(repo, "show", "-s", "--format=%B", "HEAD")
    assert message.rstrip("\n").endswith("\n\nCrucible-Attempt: HT-0007")
    # An amend runs the hook again and finds the trailer already there.
    _git(repo, "commit", "-q", "--amend", "--no-edit", env=env)
    assert _trailers(repo) == ["HT-0007"]
    _git(repo, "commit", "-q", "--amend", "-m", "Reworded", env=env)
    assert _trailers(repo) == ["HT-0007"]


def test_the_hook_keeps_a_trailer_the_worker_already_wrote(tmp_path: Path) -> None:
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    env = _git_only_path(tmp_path)
    _commit(repo, "a.txt", "Add a", "Crucible-Attempt: HT-0007", env=env)
    assert _trailers(repo) == ["HT-0007"]
    # A harness that wrote the key with another value keeps its own; never a second one.
    _commit(repo, "b.txt", "Add b", "Crucible-Attempt: attempt-42", env=env)
    assert _trailers(repo) == ["attempt-42"]
    # The key only in prose is not a trailer, so the hook still adds one.
    _commit(repo, "c.txt", "Add c", "Crucible-Attempt: in prose\nand more text", env=env)
    assert _trailers(repo) == ["HT-0007"]


def test_the_hook_is_skipped_by_no_verify(tmp_path: Path) -> None:
    """What the commit_policy gate is for: the hook is a convenience, the gate is the
    check, and `--no-verify` is exactly how a worker gets past the first."""
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "--no-verify", "-m", "Add a")
    assert _trailers(repo) == []


def test_the_preparer_points_the_checkout_at_crucibles_hooks_only() -> None:
    from tests.unit.test_scripts import preparer  # noqa: PLC0415

    script = preparer("crucible/test")
    assert 'config core.hooksPath "$IDENTITY_MOUNT/hooks"' in script
    assert f"IDENTITY_MOUNT='{IDENTITY_MOUNT}'" in script
    # The preparer's own git still runs with no hooks at all.
    assert "-c core.hooksPath=/dev/null" in script


def test_identity_md_names_the_exact_trailer_and_says_crucible_adds_it(tmp_path: Path) -> None:
    text = (_bundle(tmp_path) / "IDENTITY.md").read_text(encoding="utf-8")
    section = text.split("## 10. Commits", 1)[1]
    assert "`Crucible-Attempt: HT-0007`" in section
    assert "`commit-msg` hook adds the trailer" in section
    assert "--no-verify" in section
    assert "`commit_policy` gate" in section
    policy_md = (tmp_path / "identity" / "policy.md").read_text(encoding="utf-8")
    assert "- commit_policy" in policy_md


def test_kubernetes_projects_the_hook_executable_and_the_rest_read_only() -> None:
    assert kubernetes._identity_item("hooks__commit-msg", "hooks/commit-msg") == {
        "key": "hooks__commit-msg",
        "path": "hooks/commit-msg",
        "mode": 0o555,
    }
    assert kubernetes._identity_item("IDENTITY.md", "IDENTITY.md") == {
        "key": "IDENTITY.md",
        "path": "IDENTITY.md",
    }


def test_a_policy_cannot_list_the_enforced_gate() -> None:
    with pytest.raises(ValueError, match="always run before review"):
        Gates(pre_pr=["commit_policy"], publication=[], post_pr=[], skipped=[])


# ----- the collector runs the publisher's check ---------------------------------------


def _collect(tmp_path: Path, repo: Path) -> Path:
    output = tmp_path / "output"
    report = tmp_path / "report"
    output.mkdir()
    report.mkdir()
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        author_email=AUTHOR,
        commit_trailer="Crucible-Attempt",
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return output


def _worker_branch(tmp_path: Path) -> Path:
    repo = _checkout(tmp_path, None)
    _git(
        repo,
        "-c",
        "user.email=someone@upstream.test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    return repo


def test_the_collector_records_a_commit_without_the_trailer(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    _commit(repo, "greet.sh", "Add greet.sh, tests/test_greet.sh, and Makefile")
    head = _git(repo, "rev-parse", "HEAD").strip()
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert check.trailer_problems == (head,)
    assert check.author_problems == ()


def test_the_collector_records_a_commit_by_another_author(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(
        repo,
        "-c",
        "user.email=someone@elsewhere.test",
        "commit",
        "-q",
        "-m",
        "Add a",
        "-m",
        "Crucible-Attempt: HT-0007",
    )
    head = _git(repo, "rev-parse", "HEAD").strip()
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert check.author_problems == ((head, "someone@elsewhere.test"),)
    assert check.trailer_problems == ()


def test_the_collector_passes_commits_the_hook_made(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    _git(repo, "config", "core.hooksPath", str(_bundle(tmp_path) / "hooks"))
    _commit(repo, "a.txt", "Add a")
    _commit(repo, "b.txt", "Add b")
    output = _collect(tmp_path, repo)
    check = read_commit_policy(output / "commit-policy")
    assert check is not None
    assert check.author_problems == ()
    assert check.trailer_problems == ()
    # The base commit, by someone else and with no trailer, is not this attempt's work.
    assert (output / "commits.txt").read_text().strip() == "2"


def test_an_unfinished_check_reads_as_not_checked(tmp_path: Path) -> None:
    directory = tmp_path / "commit-policy"
    directory.mkdir()
    (directory / "trailer-problems.txt").write_text("", encoding="utf-8")
    assert read_commit_policy(directory) is None
    assert read_commit_policy(tmp_path / "absent") is None


def test_the_publisher_and_the_collector_run_one_rule() -> None:
    """Not a fork: the same function text, differing only in how each calls git."""
    publisher = scripts.publisher_script(
        clone_url="https://github.com/o/r.git",
        work_branch="crucible/test",
        base_ref="main",
        expected_head="a" * 40,
        author_name="crucible-worker",
        author_email=AUTHOR,
        commit_trailer="Crucible-Attempt",
    )
    collector = scripts.collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1024
    )
    assert scripts._commit_policy_check("git") in publisher
    assert scripts._commit_policy_check(scripts.GIT + ' -C "$REPO"') in collector
    assert 'commit_policy_check "$RANGE" "$OUT"' in publisher


def test_the_hook_puts_the_trailer_last_even_after_a_dashed_line(tmp_path: Path) -> None:
    """A `---` line in a body is prose. Without `--no-divider`, git reads it as the start
    of a patch and puts the trailer above it, where no check finds it."""
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    _commit(repo, "a.txt", "Add a", "Notes:\n\n---\n\nMore notes", env=_git_only_path(tmp_path))
    assert _trailers(repo) == ["HT-0007"]
    message = _git(repo, "show", "-s", "--format=%B", "HEAD")
    assert message.rstrip("\n").endswith("More notes\n\nCrucible-Attempt: HT-0007")


def test_the_check_reports_a_range_it_cannot_read_as_not_checked(tmp_path: Path) -> None:
    """A check that did not run is never read as one that found nothing."""
    repo = _worker_branch(tmp_path)
    _commit(repo, "a.txt", "Add a")
    # A remote-tracking ref that names an object the checkout does not have.
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "crucible" / "test").parent.mkdir()
    (repo / ".git" / "refs" / "remotes" / "origin" / "crucible" / "test").write_text(
        "f" * 40 + "\n", encoding="utf-8"
    )
    output = _collect(tmp_path, repo)
    assert read_commit_policy(output / "commit-policy") is None


def test_the_publisher_refuses_when_it_cannot_read_the_commits(tmp_path: Path) -> None:
    """The publisher side of the same rule: no push on a check that did not run."""
    repo = _worker_branch(tmp_path)
    out = tmp_path / "publish"
    out.mkdir()
    body = scripts._commit_policy_check("git") + 'commit_policy_check "nosuchref..HEAD" "$OUT"'
    result = subprocess.run(
        ["sh", "-c", f"cd {repo}; OUT={out}; POLICY_AUTHOR_EMAIL={AUTHOR}; TRAILER=X; {body}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    publisher = scripts.publisher_script(
        clone_url="https://github.com/o/r.git",
        work_branch="crucible/test",
        base_ref="main",
        expected_head="a" * 40,
        author_name="crucible-worker",
        author_email=AUTHOR,
        commit_trailer="Crucible-Attempt",
    )
    refusal = publisher.split('if ! commit_policy_check "$RANGE" "$OUT"; then', 1)[1]
    assert refusal.split("fi\n", 1)[0].rstrip().endswith("exit 6")


def test_the_collector_never_verifies_a_signature_a_worker_planted(tmp_path: Path) -> None:
    """A worker-written `.git/config` with `log.showSignature` and a `gpg.program` of its
    choosing must not run that program from the collector's `git show`."""
    repo = _worker_branch(tmp_path)
    sentinel = tmp_path / "gpg-ran"
    program = tmp_path / "fake-gpg"
    program.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    program.chmod(0o755)
    _commit(repo, "a.txt", "Add a", "Crucible-Attempt: HT-0007")
    # A commit object with a gpgsig header, written by hand.
    raw = _git(repo, "cat-file", "commit", "HEAD")
    header, _, message = raw.partition("\n\n")
    signature = "-----BEGIN PGP SIGNATURE-----\n x\n -----END PGP SIGNATURE-----"
    signed = f"{header}\ngpgsig {signature}\n\n{message}"
    sha = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        input=signed,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/heads/crucible/test", sha)
    _git(repo, "config", "log.showSignature", "true")
    _git(repo, "config", "gpg.program", str(program))
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert not sentinel.exists()
