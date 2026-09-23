#!/usr/bin/env python3
"""Which of a set of tags is the highest published release version.

    version.py <candidate> [tag ...]
    printf '%s\\n' "$TAGS" | version.py <candidate>

The release's "move latest only if this is the highest published version" step
(`.github/workflows/release.yml`) calls this rule once, before moving the service
image's `latest` and the worker images' `latest` and `script-harness-latest` together:
`latest` follows the highest published version, never merely the most
recent push, so re-publishing an old fix or re-running an old release job never moves
it backwards.

Only a tag that is exactly `N.N.N` (all-numeric components) counts as a version;
anything else, a build-id tag, a harness-prefixed tag such as
`script-harness-1.0.0`, or `latest` itself, is ignored.

`candidate` is always included in the comparison pool, so the answer is never empty
even when nothing is published yet.
"""

from __future__ import annotations

import re
import sys

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def is_version(tag: str) -> bool:
    return VERSION.fullmatch(tag) is not None


def version_key(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a release version")
    first, second, third = match.groups()
    return (int(first), int(second), int(third))


def highest_version(candidate: str, tags: list[str]) -> str:
    """The highest version among `candidate` and every tag in `tags` that is itself a
    bare version; a tag that is not a version never enters the comparison."""
    versions = [tag for tag in tags if is_version(tag)]
    versions.append(candidate)
    return max(versions, key=version_key)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("version: usage: version.py <candidate> [tag ...]", file=sys.stderr)
        return 2
    candidate, *tags = argv
    if not tags:
        tags = [line.strip() for line in sys.stdin if line.strip()]
    print(highest_version(candidate, tags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
