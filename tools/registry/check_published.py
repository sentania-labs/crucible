"""Resolve a published worker image through the Kubernetes provider's registry adapter.

The proof that the adapter reads the real registry the release publishes to (108): an
anonymous pull of the reference, which must come back with a sha256 digest and a
version label for every harness it is expected to carry. No credential is used or
needed; the package is public.

CI runs it with the pinned crane on PATH (`make registry-check`). The release runs it
inside the service image it has just built, so it also proves the image ships crane:

    python tools/registry/check_published.py \\
      --reference ghcr.io/sentania-labs/crucible-worker:latest \\
      --expect-harness claude_code --expect-harness codex
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from crucible.adapters.execution.k8sregistry import CraneRegistryClient, RegistryError


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--expect-harness", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        info = CraneRegistryClient(timeout=args.timeout).resolve(args.reference)
    except RegistryError as exc:
        print(f"check-published: {args.reference} did not resolve: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"reference": args.reference, "pinned": info.reference, "harnesses": info.harnesses},
            indent=2,
        )
    )
    problems = []
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", info.digest):
        problems.append(f"the digest {info.digest!r} is not a sha256 digest")
    if not info.harnesses:
        problems.append("the image carries no crucible.harnesses label")
    for harness in args.expect_harness:
        if not info.version_of(harness):
            problems.append(f"no version label for the {harness} harness")
    for problem in problems:
        print(f"check-published: {args.reference}: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
