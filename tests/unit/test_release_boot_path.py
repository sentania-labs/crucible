"""The release and CI Compose boots must remain Makefile-owned."""

from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
RELEASE = REPOSITORY / ".github" / "workflows" / "release.yml"
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPOSITORY / "Makefile"


def workflow_step_run(workflow: Path, job: str, name: str) -> str:
    document = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = document["jobs"][job]["steps"]
    for step in steps:
        if step.get("name") == name:
            run = step.get("run")
            if isinstance(run, str):
                return run
            raise AssertionError(f"{workflow} step {name!r} has no run command")
    raise AssertionError(f"{workflow} has no {job!r} step named {name!r}")


def test_release_and_ci_boot_through_the_shared_make_target() -> None:
    release = RELEASE.read_text(encoding="utf-8")
    ci = CI.read_text(encoding="utf-8")
    release_boot = workflow_step_run(
        RELEASE, "release", "boot the built image and prove it is the candidate"
    )
    ci_boot = workflow_step_run(CI, "compose-smoke", "make up from a clean clone")

    assert "docker compose up" not in release
    assert "docker compose up" not in ci
    assert 'make up COMPOSE_UP_FLAGS="--pull never --no-build"' in release_boot
    assert ci_boot.strip() == "make up"


def test_make_up_defaults_to_build_and_accepts_release_pull_policy() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "COMPOSE_UP_FLAGS ?= --build" in makefile
    assert "$(COMPOSE) up -d $(COMPOSE_UP_FLAGS) --wait" in makefile
