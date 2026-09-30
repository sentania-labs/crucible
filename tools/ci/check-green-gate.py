#!/usr/bin/env python3
"""Fail when a job in .github/workflows/ci.yml is not in the `green` job's `needs`.

The ruleset on main requires one check, `green`, which passes only when every job it
needs succeeded. A job added to the workflow without being added to that list would run
without being required, so `make lint` runs this and refuses the omission.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")
GATE = "green"


def main() -> int:
    text = WORKFLOW.read_text()
    jobs_block = text.split("\njobs:\n", 1)[1]
    jobs = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", jobs_block, flags=re.MULTILINE)
    gate = re.search(
        rf"^  {GATE}:\n(?:.*\n)*?\s+needs:\s*\[([^\]]*)\]", jobs_block, flags=re.MULTILINE
    )
    if gate is None:
        print(f"{WORKFLOW}: no `{GATE}` job with a bracketed `needs` list", file=sys.stderr)
        return 1
    needed = {item.strip() for item in gate.group(1).split(",") if item.strip()}
    expected = set(jobs) - {GATE}
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
