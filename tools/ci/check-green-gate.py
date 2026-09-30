#!/usr/bin/env python3
"""Fail when a job in .github/workflows/ci.yml is not in the `green` job's `needs`.

The ruleset on main requires one check, `green`, which passes only when every job it
needs succeeded. A job added to the workflow without being added to that list would run
without being required, so `make lint` runs this and refuses the omission.

The workflow is parsed as YAML, so every job declaration counts however it is written
(quoted keys and inline mappings included), and a `needs` written as a list or as a
single string is read the same way GitHub reads it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")
GATE = "green"


def main() -> int:
    document = yaml.safe_load(WORKFLOW.read_text())
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        print(f"{WORKFLOW}: no `jobs` mapping", file=sys.stderr)
        return 1
    gate = jobs.get(GATE)
    if not isinstance(gate, dict) or "needs" not in gate:
        print(f"{WORKFLOW}: no `{GATE}` job with a `needs` list", file=sys.stderr)
        return 1
    raw_needs = gate["needs"]
    needs = [raw_needs] if isinstance(raw_needs, str) else list(raw_needs or [])
    needed = {str(item) for item in needs}
    expected = {str(name) for name in jobs} - {GATE}
    missing = sorted(expected - needed)
    extra = sorted(needed - expected)
    for name in missing:
        print(
            f"{WORKFLOW}: job `{name}` is not in `{GATE}.needs`; every job must be", file=sys.stderr
        )
    for name in extra:
        print(f"{WORKFLOW}: `{GATE}.needs` names `{name}`, which is not a job", file=sys.stderr)
    if missing or extra:
        return 1
    print(f"{GATE} gates all {len(expected)} jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
