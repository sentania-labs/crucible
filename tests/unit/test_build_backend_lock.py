"""The service image's build backend is hashed and locked too (113).

uv.lock covers [project].dependencies, but never [build-system].requires: without
the build-backend dependency group in pyproject.toml, `uv sync` resolves hatchling
and hatch-vcs from the index unhashed at image build time, and a new release of
either can change or break the image the way uvicorn 0.54.0 broke the worker image.
This test keeps the group, the lock entries it produces, and the Dockerfile flags
that make the build use them from being dropped or loosened.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
PYPROJECT = REPOSITORY / "pyproject.toml"
DOCKERFILE = REPOSITORY / "Dockerfile"


def build_system_requires() -> dict[str, str]:
    """{"hatchling": ">=1.27", "hatch-vcs": ">=0.4"} from [build-system].requires."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    requires = {}
    for entry in data["build-system"]["requires"]:
        match = re.match(r"([A-Za-z0-9._-]+)\s*(.*)", entry)
        assert match, entry
        requires[match.group(1)] = match.group(2)
    return requires


def test_the_build_backend_group_mirrors_build_system_requires() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    group = data["dependency-groups"]["build-backend"]
    requires = build_system_requires()
    assert requires, "build-system.requires is empty; nothing for this test to cover"
    for name, specifier in requires.items():
        assert any(
            entry.replace(" ", "").startswith(f"{name}{specifier}".replace(" ", ""))
            for entry in group
        ), (name, specifier, group)


def test_the_build_backend_group_is_not_a_default_group() -> None:
    """A plain `uv sync` (make lint, make test) must not install the build backend:
    it exists only for the Dockerfile's explicit `--group build-backend`."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    default_groups = data.get("tool", {}).get("uv", {}).get("default-groups")
    if default_groups is not None:
        assert "build-backend" not in default_groups


def test_uv_lock_hashes_every_build_backend_package() -> None:
    lock = tomllib.loads((REPOSITORY / "uv.lock").read_text(encoding="utf-8"))
    packages = {pkg["name"]: pkg for pkg in lock["package"]}
    for name in build_system_requires():
        assert name in packages, name
        pkg = packages[name]
        artifacts = [*pkg.get("wheels", []), *([pkg["sdist"]] if "sdist" in pkg else [])]
        assert artifacts, f"{name} has no locked artifact"
        assert all("hash" in artifact for artifact in artifacts), name


def test_the_dockerfile_installs_the_build_backend_group_before_building_the_project() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    lines = text.splitlines()

    sync_group_lines = [
        line for line in lines if "uv sync" in line and "--group build-backend" in line
    ]
    assert sync_group_lines, "no `uv sync ... --group build-backend` step in the Dockerfile"
    for line in sync_group_lines:
        assert "--frozen" in line, line

    # The step that builds the project must reuse that hashed build backend instead
    # of letting uv fetch its own, unhashed, copy from the index in an isolated env.
    build_lines = [
        line for line in lines if "uv sync" in line and "--no-build-isolation-package" in line
    ]
    assert build_lines, "no `uv sync ... --no-build-isolation-package` step in the Dockerfile"
    for line in build_lines:
        assert "crucible" in line, line
        assert "--frozen" in line, line
        # Without --no-editable, hatchling's editable build reaches for the
        # undeclared, unhashed `editables` package when isolation is off.
        assert "--no-editable" in line, line
        # With no network the step cannot fetch anything, locked or not.
        assert "RUN --network=none" in line, line
        assert "--group build-backend" not in line, (
            "the project-build step must not itself request the group: "
            "leaving it out is what uninstalls the build backend again after use"
        )
