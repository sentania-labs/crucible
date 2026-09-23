#!/usr/bin/env python3
"""Render the published image digests as GitHub release notes (FDY-0088).

    release_notes.py --service-image ghcr.io/sentania-labs/crucible:0.5.0 \
        --worker-manifest images/manifest.env \
        --worker-repository ghcr.io/sentania-labs/crucible-worker

By the time this runs, the release workflow has already pushed the service image and
`images-publish` has already pushed and verified the worker image, so both are on the
registry. Every digest printed here is read back from the registry over the same OCI
distribution API `worker_images.py` uses, never taken from a local build or a manifest
file: a runbook that quoted a local digest could describe an image nobody can pull.

Prints markdown to stdout; the release workflow puts it in front of `gh release
create --notes-file` and `--generate-notes` appends the usual changelog after it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from worker_images import PublishError, Registry, declared_images


def render(service_ref: str, service_digest: str, worker_ref: str, worker_digest: str) -> str:
    return (
        "## Published image digests\n"
        "\n"
        "Read back from the registry at release time; if this release predates this "
        "section, `docs/deployment.md` gives the equivalent registry command.\n"
        "\n"
        f"- `{service_ref}@{service_digest}`\n"
        f"- `{worker_ref}@{worker_digest}`\n"
    )


def worker_image(manifest: Path) -> tuple[str, str]:
    for image in declared_images(manifest):
        if image.key == "WORKER":
            return image.tag, image.digest
    raise PublishError(f"{manifest} declares no WORKER image")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--service-image", required=True, help="repository:tag")
    parser.add_argument("--worker-manifest", type=Path, required=True)
    parser.add_argument("--worker-repository", required=True)
    args = parser.parse_args(argv)
    try:
        service_repository, sep, service_tag = args.service_image.rpartition(":")
        if not sep:
            raise PublishError(f"--service-image {args.service_image!r} names no tag")
        service_digest = Registry(service_repository).published_digest(service_tag)
        if service_digest is None:
            raise PublishError(f"{args.service_image} is not published")

        worker_tag, declared_worker_digest = worker_image(args.worker_manifest)
        worker_digest = Registry(args.worker_repository).published_digest(worker_tag)
        if worker_digest != declared_worker_digest:
            raise PublishError(
                f"{args.worker_repository}:{worker_tag} carries {worker_digest or 'nothing'} "
                f"on the registry, not the declared {declared_worker_digest}"
            )

        sys.stdout.write(
            render(
                args.service_image,
                service_digest,
                f"{args.worker_repository}:{worker_tag}",
                worker_digest,
            )
        )
    except PublishError as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
