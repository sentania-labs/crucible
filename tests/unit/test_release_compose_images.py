"""The release image guard follows resolved Compose images, not service names."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "compose_images.py"
CANDIDATE = "ghcr.io/sentania-labs/crucible:classification-test"


def invoke(
    mode: str = "classify",
    *,
    compose_file: Path | None = None,
    compose_command: str = "docker compose",
    docker_command: str = "docker",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE": compose_command,
            "CRUCIBLE_IMAGE": CANDIDATE,
            "DOCKER": docker_command,
            "POSTGRES_PASSWORD": "classification-only",
        }
    )
    environment.update(extra_environment or {})
    if compose_file is not None:
        environment["COMPOSE_FILE"] = str(compose_file)
    else:
        environment.pop("COMPOSE_FILE", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), mode],
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_repository_candidate_services_are_explicit() -> None:
    result = invoke()
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

    result = invoke(compose_file=compose_file)

    assert result.returncode == 1
    assert "service 'no-image' has no resolved image" in result.stderr


def fake_commands(tmp_path: Path) -> tuple[Path, Path, Path]:
    log = tmp_path / "commands.log"
    compose = tmp_path / "compose.py"
    compose.write_text(
        """\
#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a", encoding="utf-8") as stream:
    stream.write("compose " + " ".join(args) + "\\n")
if "config" in args:
    candidate = os.environ["CRUCIBLE_IMAGE"]
    print(json.dumps({"services": {
        "candidate-one": {"image": candidate},
        "candidate-two": {"image": candidate},
        "supporting": {"image": "example.invalid/supporting:1"},
    }}))
elif args[:2] == ["pull", "--quiet"]:
    pass
elif args[:3] == ["ps", "--all", "--quiet"]:
    print("cid-" + args[3])
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    compose.chmod(0o755)

    docker = tmp_path / "docker.py"
    docker.write_text(
        """\
#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a", encoding="utf-8") as stream:
    stream.write("docker " + " ".join(args) + "\\n")
if args[:2] == ["image", "inspect"] or args[:1] == ["inspect"]:
    print("sha256:local-candidate")
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return compose, docker, log


def test_pull_mode_passes_only_supporting_services_to_compose(tmp_path: Path) -> None:
    compose, docker, log = fake_commands(tmp_path)
    result = invoke(
        "pull-supporting",
        compose_command=str(compose),
        docker_command=str(docker),
        extra_environment={"COMMAND_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    pulls = [line for line in log.read_text(encoding="utf-8").splitlines() if " pull " in line]
    assert pulls == ["compose pull --quiet supporting"]


def test_verify_mode_checks_every_candidate_with_exited_containers(tmp_path: Path) -> None:
    compose, docker, log = fake_commands(tmp_path)
    result = invoke(
        "verify-candidate",
        compose_command=str(compose),
        docker_command=str(docker),
        extra_environment={"COMMAND_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "compose ps --all --quiet candidate-one" in commands
    assert "compose ps --all --quiet candidate-two" in commands
    assert "compose ps --all --quiet supporting" not in commands
    assert "docker inspect cid-candidate-one --format {{.Image}}" in commands
    assert "docker inspect cid-candidate-two --format {{.Image}}" in commands
