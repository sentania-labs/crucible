"""The release image guard follows resolved Compose images, not service names."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "compose_images.py"
CANDIDATE = "ghcr.io/sentania-labs/crucible:classification-test"


def classify(*, compose_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE": "docker compose",
            "CRUCIBLE_IMAGE": CANDIDATE,
            "POSTGRES_PASSWORD": "classification-only",
        }
    )
    if compose_file is not None:
        environment["COMPOSE_FILE"] = str(compose_file)
    else:
        environment.pop("COMPOSE_FILE", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "classify"],
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_repository_candidate_services_are_explicit() -> None:
    result = classify()
    assert result.returncode == 0, result.stderr
    candidate_services = {
        line.split(maxsplit=2)[1]
        for line in result.stdout.splitlines()
        if line.startswith("candidate ")
    }
    assert candidate_services == {"credential-init", "crucible", "migrate"}


def test_a_service_without_an_image_fails_loudly(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        """\
name: missing-image-test
services:
  candidate:
    image: ${CRUCIBLE_IMAGE}
  no-image:
    build: .
""",
        encoding="utf-8",
    )

    result = classify(compose_file=compose_file)

    assert result.returncode == 1
    assert "service 'no-image' has no resolved image" in result.stderr
