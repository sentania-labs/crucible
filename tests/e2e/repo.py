"""Throwaway origin repositories for the e2e tier.

Each one carries the `e2e-behavior` file the script harness reads and three check
scripts standing in for the repository's own `make lint`, `make test`, `make scan`.
The repositories live inside the artifact root so the preparer container can clone
from them with no network at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CHECKS = {
    "lint.sh": "echo lint ok\n",
    "test.sh": "echo test ok\n",
    "scan.sh": "echo scan ok\n",
}

GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "e2e",
    "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
    "GIT_COMMITTER_NAME": "e2e",
    "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), env=GIT_ENV, check=True, capture_output=True)


def make_origin(
    root: Path, name: str, behavior: str = "succeed", *, extra: dict[str, str] | None = None
) -> str:
    """Build a bare origin and return the url (a path inside the artifact root)."""
    root.mkdir(parents=True, exist_ok=True)
    work = root / f"{name}-work"
    bare = root / f"{name}.git"
    for path in (work, bare):
        if path.exists():
            subprocess.run(["rm", "-rf", str(path)], check=True)
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "e2e-behavior").write_text(behavior + "\n", encoding="utf-8")
    (work / "README.md").write_text(f"# {name}\n\nA throwaway e2e repository.\n", encoding="utf-8")
    (work / "src").mkdir()
    (work / "src" / "app.txt").write_text("base\n", encoding="utf-8")
    checks = work / "checks"
    checks.mkdir()
    for filename, body in CHECKS.items():
        (checks / filename).write_text(body, encoding="utf-8")
    for filename, body in (extra or {}).items():
        target = work / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "e2e: base commit", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=root)
    # The preparer reads this as container uid 1000. The quota checkpoint collector
    # also pushes its WIP commit back to this throwaway origin before rerouting.
    for path in bare.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)
    bare.chmod(0o777)
    return str(bare)
