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
    return [line for line in text.splitlines() if re.match(r"\s*RUN\s", line, re.I)]


# pip, pip3 or python -m pip; -r lock, -rlock, --requirement lock or --requirement=lock.
PIP_INSTALL = re.compile(r"(\bpip3?|-m\s+pip)\s+install\b")
REQUIREMENT = re.compile(r"(\s-r|\s--requirement[\s=])")


def commands(run: str) -> list[str]:
    """The shell commands of a RUN, split on ; && || and |."""
    return [part.strip() for part in re.split(r";|&&|\|\||\|", run)]


def test_every_lock_install_is_the_whole_set_with_no_resolution() -> None:
    found = 0
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        # Heredoc and exec-form RUNs would hide an install from the scan below.
        assert not re.search(r"^\s*RUN\s.*<<", text, re.M | re.I), dockerfile
        assert not re.search(r"^\s*RUN\s+\[.*pip", text, re.M | re.I), dockerfile
        for run in run_instructions(dockerfile):
            steps = commands(run)
            for index, command in enumerate(steps):
                if not (PIP_INSTALL.search(command) and REQUIREMENT.search(command)):
                    continue
                found += 1
                where = f"{dockerfile.relative_to(REPOSITORY)}: {command}"
                assert "--require-hashes" in command, where
                assert "--no-deps" in command, where
                assert "--only-binary :all:" in command, where
                after = steps[index + 1 :]
                assert any(re.search(r"\bpip3?\s+check\b", step) for step in after), where
                assert "diff -u /tmp/locked /tmp/installed" in after, where
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
