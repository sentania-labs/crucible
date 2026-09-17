"""What the live GitHub tier needs, and how it cleans up after itself (18, 23).

This tier runs locally only, against one throwaway repository, and it removes every
branch, pull request, and tag it creates. It never touches the default branch, and it
never merges anything Crucible opened except the one pull request the merge-observation
test opens for that purpose, which it merges with the App token as a test-only act
(docs/implementation-notes/c4.md).

The operator's own `gh` credentials are used read-only, for one thing: seeding a local
bare mirror of the target repository so the worker and the collector have something to
clone. Workers hold no GitHub credential, and a private repository cannot be cloned
without one, so the mirror is what keeps that rule intact on a private target.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.ports.github import InstallationToken

APP_JSON_ENV = "CRUCIBLE_GITHUB_APP_JSON"
APP_KEY_ENV = "CRUCIBLE_GITHUB_APP_KEY"
TARGET_ENV = "CRUCIBLE_GITHUB_TARGET_REPO"
API_BASE = os.environ.get("CRUCIBLE_GITHUB_API_BASE", "https://api.github.com")
BRANCH_PREFIX = "crucible/C4-"


@dataclass(frozen=True, slots=True)
class LiveConfig:
    app_id: int
    installation_id: int
    private_key_path: str
    repository: str

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[-1]

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.repository}.git"


def why_not_configured() -> str:
    """The reason this tier cannot run, or an empty string. Never a partial run."""
    app_json = os.environ.get(APP_JSON_ENV, "")
    key = os.environ.get(APP_KEY_ENV, "")
    target = os.environ.get(TARGET_ENV, "")
    if not app_json or not key or not target:
        return (
            f"set {APP_JSON_ENV}, {APP_KEY_ENV} and {TARGET_ENV} to run the live GitHub "
            "tier; it runs locally only, against a throwaway repository"
        )
    if not Path(key).is_file():
        return f"{APP_KEY_ENV} does not name a readable file"
    if not Path(app_json).is_file():
        return f"{APP_JSON_ENV} does not name a readable file"
    if shutil.which("gh") is None:
        return "gh is needed, read-only, to seed the local mirror of the target"
    if shutil.which("git") is None:
        return "git is needed to seed the local mirror of the target"
    return ""


def load_config() -> LiveConfig:
    document = json.loads(Path(os.environ[APP_JSON_ENV]).read_text(encoding="utf-8"))
    return LiveConfig(
        app_id=int(document["id"]),
        installation_id=int(document["installation_id"]),
        private_key_path=os.environ[APP_KEY_ENV],
        repository=os.environ[TARGET_ENV],
    )


def client(config: LiveConfig) -> RestGitHubClient:
    transport = RestTransport(API_BASE, timeout=30.0)
    authenticator = AppAuthenticator(
        AppConfig(
            app_id=config.app_id,
            private_key_path=config.private_key_path,
            api_base=API_BASE,
        ),
        transport,
    )
    return RestGitHubClient(authenticator, transport)


def token(github: RestGitHubClient, config: LiveConfig) -> InstallationToken:
    return github.installation_token(
        installation_id=config.installation_id, repository=config.repository
    )


def _git(*args: str, cwd: Path | None = None) -> str:
    """git with the operator's own credential helper, read-only against the target.

    `gh auth setup-git` is what makes this work without a token ever being in this
    process: git asks `gh` for the credential, and `gh` answers from the operator's own
    keyring. Nothing here reads or prints it."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return completed.stdout


def seed_mirror(root: Path, config: LiveConfig) -> str:
    """A bare mirror of the target's default branch, inside the artifact root.

    This is the only place the operator's credentials are used, and only to read. The
    worker, the preparer, and the collector clone from here; the publisher pushes to the
    real remote with the App's installation token and nothing else."""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / f"{config.name}.git"
    if bare.exists():
        shutil.rmtree(bare)
    _git(
        "clone",
        "--bare",
        "--single-branch",
        "--branch",
        "main",
        config.https_url,
        str(bare),
    )
    # The preparer runs as container uid 1000, a different host uid under the rootless
    # daemon (S9 Test E), so the mirror has to be world readable.
    for path in bare.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    bare.chmod(0o755)
    return str(bare)


def run_id() -> str:
    return uuid.uuid4().hex[:8]


def branch_for(external_id: str) -> str:
    return f"{BRANCH_PREFIX}{external_id}"


@dataclass
class Cleanup:
    """Everything this tier created on the remote, removed at the end of the run."""

    config: LiveConfig
    github: RestGitHubClient
    branches: list[str]
    pull_requests: list[int]

    def add_branch(self, branch: str) -> None:
        if branch and branch not in self.branches:
            self.branches.append(branch)

    def add_pull_request(self, number: int) -> None:
        if number and number not in self.pull_requests:
            self.pull_requests.append(number)

    def run(self) -> dict[str, Any]:
        """Close what is open and delete every branch created. Never `main`."""
        removed: dict[str, Any] = {"branches": [], "pull_requests": [], "errors": []}
        access = token(self.github, self.config)
        try:
            for number in self.pull_requests:
                try:
                    current = self.github.get_pull_request(
                        access, repository=self.config.repository, number=number
                    )
                    if current.state == "open":
                        self.github.close_pull_request(
                            access, repository=self.config.repository, number=number
                        )
                    removed["pull_requests"].append(number)
                except Exception as exc:  # cleanup reports, never raises
                    removed["errors"].append(f"pull request {number}: {exc}")
            for branch in self.branches:
                if branch in ("main", "master") or branch.startswith("release/"):
                    removed["errors"].append(f"refusing to delete {branch}")
                    continue
                try:
                    self.github.delete_ref(access, repository=self.config.repository, ref=branch)
                    removed["branches"].append(branch)
                except Exception as exc:
                    removed["errors"].append(f"branch {branch}: {exc}")
        finally:
            access.discard()
        return removed


def merge_with_app_token(
    github: RestGitHubClient, config: LiveConfig, number: int, *, sha: str
) -> tuple[int, Any]:
    """Merge one pull request, as a test-only act.

    Crucible has no merge endpoint and never will: merging is the operator's act (23).
    The merge-observation test needs a merged PR to observe, so the *test* performs it
    with the App token, through the transport, on the throwaway repository only. This
    function deliberately lives in the test tier and not in the client."""
    transport = github._http
    access = token(github, config)
    try:
        return transport.request(
            "PUT",
            f"/repos/{config.repository}/pulls/{number}/merge",
            bearer=access.reveal(),
            body={"merge_method": "squash", "sha": sha},
        )[:2]
    finally:
        access.discard()
