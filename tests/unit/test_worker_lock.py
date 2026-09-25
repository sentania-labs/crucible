"""A hash lock is installed as the whole set, never resolved at build time (FDY-0115).

Without --no-deps, pip resolves each locked package's own requirements against the
index, so a new upstream release matching an extra (uvicorn[standard] for hermes-agent
0.19.0) broke the worker image build the day it appeared. The build itself proves the
installed set equals the lock; this test keeps the flags that make that true from
being dropped, which no build would notice until the next upstream release.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILES = [*sorted(REPOSITORY.glob("images/*/Dockerfile")), REPOSITORY / "Dockerfile"]


def run_instructions(dockerfile: Path) -> list[str]:
    """Each RUN instruction with its backslash continuations joined."""
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    return [line for line in text.splitlines() if re.match(r"\s*RUN\s", line)]


def lock_installs(run: str) -> list[str]:
    return [
        command
        for command in run.split(";")
        if "pip install" in command and re.search(r"\s(-r|--requirement)\s", command)
    ]


def test_every_lock_install_is_the_whole_set_with_no_resolution() -> None:
    found = 0
    for dockerfile in DOCKERFILES:
        for run in run_instructions(dockerfile):
            for command in lock_installs(run):
                found += 1
                where = f"{dockerfile.relative_to(REPOSITORY)}: {command.strip()}"
                assert "--require-hashes" in command, where
                assert "--no-deps" in command, where
                assert "--only-binary :all:" in command, where
                assert "pip check" in run, where
                assert "diff -u /tmp/locked /tmp/installed" in run, where
    # The worker image's Hermes venv is the one lock install today; a scan that finds
    # none has stopped looking, not proved anything.
    assert found >= 1


def test_the_hermes_lock_names_every_package_with_a_hash() -> None:
    """Each name==version line is followed by its hashes, and the lock carries the
    hermes-agent version the Dockerfile pins, so the freeze comparison has a real list."""
    worker = REPOSITORY / "images" / "worker"
    lock = (worker / "requirements.lock").read_text(encoding="utf-8")
    hashed = re.findall(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) \\\n\s+--hash=sha256:[0-9a-f]{64}",
        lock,
        re.M,
    )
    named = re.findall(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", lock, re.M)
    assert hashed
    assert len(hashed) == len(named)
    pinned = re.search(
        r"^ARG HARNESS_HERMES_VERSION=(\S+)$",
        (worker / "Dockerfile").read_text(encoding="utf-8"),
        re.M,
    )
    assert pinned
    assert ("hermes-agent", pinned.group(1)) in hashed
