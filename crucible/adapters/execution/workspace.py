"""Workspace preparation for the Docker provider (08).

Crucible does the clone itself, as its own process, before any worker exists. What the
worker gets is a checkout with no usable remote, an author identity from policy, no
credential helper, the shims listed in `.git/info/exclude`, a read-only identity bundle
and an empty report directory.

Git runs with a scrubbed environment everywhere: no global or system config, no
terminal prompt, no external diff, no hooks, no pager.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The origin URL a worker sees. It resolves nowhere, so a push cannot even start (S4).
ORIGIN_PLACEHOLDER = "crucible-no-remote://this-checkout-cannot-push"
SHIM_NAMES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
EXCLUDE_ENTRIES: tuple[str, ...] = (
    "# Written by Crucible at prepare; these are shims, not work (06, 11).",
    "/CLAUDE.md",
    "/AGENTS.md",
    "/.crucible/",
)

GIT_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_ALLOW_PROTOCOL": "file:https:http:ssh:git",
    "GIT_LFS_SKIP_SMUDGE": "1",
}
GIT_FLAGS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.file.allow=always",
)


class WorkspaceError(Exception):
    """Preparation failed. The attempt is an `environment` failure (16)."""


@dataclass(frozen=True, slots=True)
class GitResult:
    exit_code: int
    stdout: str
    stderr: str


def git(*args: str, cwd: Path | None = None, timeout: float = 300.0) -> GitResult:
    """Run git with the scrubbed environment. Never raises on a non-zero exit."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        **GIT_ENV,
    }
    try:
        completed = subprocess.run(
            ["git", *GIT_FLAGS, *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard requirement
        raise WorkspaceError("git is not installed in the Crucible image") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    return GitResult(completed.returncode, completed.stdout, completed.stderr)


def git_or_raise(*args: str, cwd: Path | None = None, timeout: float = 300.0) -> str:
    result = git(*args, cwd=cwd, timeout=timeout)
    if result.exit_code != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed with {result.exit_code}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def refresh_cache(cache_root: Path, url: str, name: str) -> Path | None:
    """Keep a bare mirror Crucible clones from with `--reference --dissociate`.

    Nothing of the cache is ever mounted into a worker: `--dissociate` copies the
    objects the checkout needs and leaves no alternates link behind (08).
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / f"{name}.git"
    if cache.exists():
        result = git("--git-dir", str(cache), "fetch", "--prune", "origin", timeout=600.0)
        if result.exit_code == 0:
            return cache
        # A cache that cannot refresh is a cache that must not be trusted.
        shutil.rmtree(cache, ignore_errors=True)
    result = git("clone", "--mirror", url, str(cache), timeout=1800.0)
    if result.exit_code != 0:
        return None
    return cache


def clone(
    *,
    url: str,
    destination: Path,
    cache: Path | None,
    base_ref: str,
    work_branch: str,
    from_remote_branch: bool,
) -> str:
    """Clone and position the checkout. Returns what it started from (08)."""
    args = ["clone", "--no-checkout"]
    if cache is not None:
        args += ["--reference", str(cache), "--dissociate"]
    args += [url, str(destination)]
    git_or_raise(*args, timeout=1800.0)

    started_from: str
    if from_remote_branch and _remote_has(destination, work_branch):
        git_or_raise("checkout", "-B", work_branch, f"origin/{work_branch}", cwd=destination)
        started_from = f"origin/{work_branch}"
    else:
        target = _resolve_base(destination, base_ref)
        git_or_raise("checkout", "-B", work_branch, target, cwd=destination)
        started_from = base_ref
    return started_from


def _remote_has(repo: Path, branch: str) -> bool:
    result = git("rev-parse", "--verify", f"refs/remotes/origin/{branch}", cwd=repo)
    return result.exit_code == 0


def _resolve_base(repo: Path, base_ref: str) -> str:
    for candidate in (f"refs/remotes/origin/{base_ref}", base_ref):
        if git("rev-parse", "--verify", candidate, cwd=repo).exit_code == 0:
            return candidate
    raise WorkspaceError(f"base ref {base_ref!r} does not exist in the clone")


def seal(repo: Path, *, author_name: str, author_email: str) -> None:
    """Remove the push path and install the author identity from policy (08)."""
    git_or_raise("remote", "set-url", "origin", ORIGIN_PLACEHOLDER, cwd=repo)
    git_or_raise("remote", "set-url", "--push", "origin", ORIGIN_PLACEHOLDER, cwd=repo)
    git_or_raise("config", "user.name", author_name, cwd=repo)
    git_or_raise("config", "user.email", author_email, cwd=repo)
    # No credential helper, and no chance of one being inherited.
    git_or_raise("config", "credential.helper", "", cwd=repo)
    git_or_raise("config", "http.extraHeader", "", cwd=repo)


def write_shims(repo: Path, identity_mount: str) -> list[str]:
    """Write the harness shims the checkout lacks and exclude them from the diff (06)."""
    written: list[str] = []
    for name in SHIM_NAMES:
        target = repo / name
        if target.exists():
            continue
        target.write_text(
            f"Read {identity_mount}/IDENTITY.md first; it is the task contract for this run.\n",
            encoding="utf-8",
        )
        written.append(name)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [line for line in EXCLUDE_ENTRIES if line not in existing]
    if missing:
        exclude.write_text(
            existing
            + ("" if existing.endswith("\n") or not existing else "\n")
            + "\n".join(missing)
            + "\n",
            encoding="utf-8",
        )
    return written


def head_sha(repo: Path) -> str:
    return git_or_raise("rev-parse", "HEAD", cwd=repo).strip()
