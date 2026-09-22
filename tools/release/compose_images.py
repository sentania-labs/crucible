#!/usr/bin/env python3
"""Classify and verify images used by the release Compose stack.

The release candidate is identified by the resolved image value, never by a
service name. This keeps the registry pull guard and the post-boot identity
check on the same definition when services are added or renamed.

Stdlib only, because the release and compose-smoke jobs do not install the
project's Python environment.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class ReleaseImageError(Exception):
    """A release image check failed with an operator-readable reason."""


@dataclass(frozen=True)
class ServiceImages:
    candidate: tuple[tuple[str, str], ...]
    supporting: tuple[tuple[str, str], ...]


def command_from_env(name: str, default: str) -> list[str]:
    command = shlex.split(os.environ.get(name, default))
    if not command:
        raise ReleaseImageError(f"{name} resolved to an empty command")
    return command


def run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or "(no output)"
        raise ReleaseImageError(f"`{' '.join(command)}` exited {completed.returncode}.\n{detail}")
    return completed.stdout


def compose(args: list[str]) -> str:
    return run(command_from_env("COMPOSE", "docker compose") + args)


def docker(args: list[str]) -> str:
    return run(command_from_env("DOCKER", "docker") + args)


def resolved_service_images() -> ServiceImages:
    candidate_image = os.environ.get("CRUCIBLE_IMAGE", "").strip()
    if not candidate_image:
        raise ReleaseImageError("CRUCIBLE_IMAGE must name the release candidate")

    raw = compose(["--profile", "*", "config", "--format", "json"])
    try:
        project: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseImageError(f"docker compose config did not return JSON: {exc}") from None

    services = project.get("services") if isinstance(project, dict) else None
    if not isinstance(services, dict) or not services:
        raise ReleaseImageError("docker compose config returned no services")

    candidate: list[tuple[str, str]] = []
    supporting: list[tuple[str, str]] = []
    for name in sorted(services):
        service = services[name]
        image = service.get("image") if isinstance(service, dict) else None
        if not isinstance(image, str) or not image.strip():
            raise ReleaseImageError(
                f"service {name!r} has no resolved image; every release service must declare one"
            )
        entry = (str(name), image)
        if image == candidate_image:
            candidate.append(entry)
        else:
            supporting.append(entry)

    if not candidate:
        raise ReleaseImageError(
            f"no service resolves to CRUCIBLE_IMAGE {candidate_image!r}; "
            "refusing to pull every image"
        )
    return ServiceImages(tuple(candidate), tuple(supporting))


def print_classification(images: ServiceImages) -> None:
    for name, image in images.candidate:
        print(f"candidate {name} {image}", flush=True)
    for name, image in images.supporting:
        print(f"supporting {name} {image}", flush=True)


def pull_supporting(images: ServiceImages) -> None:
    print_classification(images)
    for name, _image in images.supporting:
        print(f"pulling supporting image for {name}", flush=True)
        compose(["pull", "--quiet", name])


def verify_candidate(images: ServiceImages) -> None:
    candidate_image = os.environ["CRUCIBLE_IMAGE"]
    built_id = docker(["image", "inspect", candidate_image, "--format", "{{.Id}}"]).strip()
    if not built_id:
        raise ReleaseImageError(f"candidate image {candidate_image!r} has no image ID")

    for name, _image in images.candidate:
        container_ids = [
            value
            for value in compose(["ps", "--all", "--quiet", name]).splitlines()
            if value.strip()
        ]
        if not container_ids:
            raise ReleaseImageError(
                f"candidate service {name!r} has no container, running or exited"
            )
        for container_id in container_ids:
            actual_id = docker(["inspect", container_id, "--format", "{{.Image}}"]).strip()
            print(f"{name} container {container_id[:12]} image: {actual_id}", flush=True)
            if actual_id != built_id:
                raise ReleaseImageError(
                    f"candidate service {name!r} uses image {actual_id}, "
                    f"not the freshly built {built_id}"
                )
    print("all candidate-image service containers use the freshly built image", flush=True)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "classify",
        "pull-supporting",
        "verify-candidate",
    }:
        print(
            f"usage: {sys.argv[0]} classify|pull-supporting|verify-candidate",
            file=sys.stderr,
        )
        return 2

    try:
        images = resolved_service_images()
        if sys.argv[1] == "classify":
            print_classification(images)
        elif sys.argv[1] == "pull-supporting":
            pull_supporting(images)
        else:
            verify_candidate(images)
    except ReleaseImageError as exc:
        print(f"release image check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
