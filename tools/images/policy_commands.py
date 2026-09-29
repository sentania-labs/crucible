"""Prove the worker image carries the program each shipped policy's checks start with (#181).

A policy's `repository.required_checks` are commands every task contract must carry as
required verification, and the worker runs them inside the worker image. The image and
the policies are versioned apart, so nothing stopped them disagreeing: default-software
required `make lint`, `make test` and `make scan` while the image had no `make`, and
every task under it failed `verification_ran` with exit 127.

The shipped policies are every file under examples/policies and the default-software
document the migrations seed. The program of a check is its first word after any
leading `NAME=value` assignments. Each program must resolve, inside the image as its
own user and PATH, to an absolute path of an executable file: a shell builtin or a
function does not count.

A policy may also name, in `repository.required_programs`, the programs its checks
call beyond their first word (hades #184: `uv` and `gitleaks` behind `make lint` and
`make scan`); each of those must resolve the same way.

What this does not prove: that the check succeeds. `make lint` runs whatever the target
repository's Makefile says, and the tools those recipes call are the repository's
business, not the policy's, unless the policy declares them. A wrapper such as
`env make lint` or `sh -c '...'` would be satisfied by the wrapper alone, and a check
that runs a script from the checkout (`./scripts/check`) would be reported missing; no
shipped policy uses either form.

    python tools/images/policy_commands.py              # the WORKER image in images/manifest.env
    python tools/images/policy_commands.py --image crucible-worker:<tag>

The image must already be in the daemon DOCKER names (`make images` or
`make images-check` leaves it there). CI runs it after the images job's build
(`make images-policy-check`).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from crucible.adapters.persistence.migrations.versions import _0001_walking_skeleton as m1

REPOSITORY = Path(__file__).resolve().parents[2]
POLICIES = REPOSITORY / "examples" / "policies"
MANIFEST = REPOSITORY / "images" / "manifest.env"

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Runs inside the image: one line per program, the program and what it resolved to,
# empty when it resolves to nothing, to a builtin, or to a file that is not executable.
_PROBE = r"""
for program; do
    found=$(command -v -- "$program" 2>/dev/null) || found=""
    case "$found" in
        /*) [ -f "$found" ] && [ -x "$found" ] || found="" ;;
        *) found="" ;;
    esac
    printf '%s\t%s\n' "$program" "$found"
done
"""


def shipped_policies() -> list[tuple[str, Mapping[str, Any]]]:
    """(label, document) for every policy this repository ships."""
    policies: list[tuple[str, Mapping[str, Any]]] = [
        ("seeded default-software (migration 0001)", m1.DEFAULT_POLICY)
    ]
    for path in sorted(POLICIES.glob("*.yaml")) + sorted(POLICIES.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            policies.append((str(path.relative_to(REPOSITORY)), document))
    return policies


def program_of(check: str) -> str:
    """The program a check runs: its first word after any NAME=value assignments."""
    for word in shlex.split(check):
        if not _ASSIGNMENT.match(word):
            return word
    raise ValueError(f"required check {check!r} names no program")


def required_programs(policies: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, list[str]]:
    """Program -> the `label: check` entries that require it, and the `label:
    required_programs` entries that declare it (hades #184)."""
    programs: dict[str, list[str]] = {}
    for label, document in policies:
        repository = document.get("repository") or {}
        for check in repository.get("required_checks") or []:
            programs.setdefault(program_of(str(check)), []).append(f"{label}: {check}")
        for program in repository.get("required_programs") or []:
            programs.setdefault(str(program), []).append(f"{label}: required_programs")
    return programs


def worker_image(manifest: Path = MANIFEST) -> str:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("WORKER="):
            return line.partition("=")[2].strip()
    raise SystemExit(f"policy_commands.py: no WORKER= line in {manifest}")


def resolve(docker: list[str], image: str, programs: Iterable[str]) -> dict[str, str]:
    """Program -> absolute path inside the image, or "" when it does not resolve."""
    names = sorted(programs)
    result = subprocess.run(
        [
            *docker,
            *("run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image),
            *("-c", _PROBE, "sh", *names),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"policy_commands.py: probing {image} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    found = dict.fromkeys(names, "")
    for line in result.stdout.splitlines():
        program, _, path = line.partition("\t")
        if program in found:
            found[program] = path.strip()
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=None, help="default: WORKER in images/manifest.env")
    args = parser.parse_args(argv)
    image = args.image or worker_image()
    docker = shlex.split(os.environ.get("DOCKER") or "docker")

    programs = required_programs(shipped_policies())
    if not programs:
        print("policy_commands.py: no shipped policy requires a check; nothing to prove")
        return 0
    found = resolve(docker, image, programs)
    missing = False
    for program in sorted(programs):
        if found[program]:
            print(f"ok       {program} -> {found[program]}")
        else:
            missing = True
            print(f"MISSING  {program}, required by:", file=sys.stderr)
            for source in programs[program]:
                print(f"           {source}", file=sys.stderr)
    if missing:
        print(
            f"policy_commands.py: {image} lacks a program a shipped policy requires",
            file=sys.stderr,
        )
        return 1
    print(f"policy_commands.py: {image} carries the program every shipped policy check starts with")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
