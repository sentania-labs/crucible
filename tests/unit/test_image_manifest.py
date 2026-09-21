"""The declared image pins must match the build inputs without building images."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
HARNESSES = ("agy", "claude_code", "codex", "hermes", "script-harness")


def image_copy(tmp_path: Path) -> Path:
    target = tmp_path / "images"
    shutil.copytree(
        REPOSITORY / "images",
        target,
        ignore=shutil.ignore_patterns("out"),
    )
    return target


def check(images: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(images / "check-manifest.sh")],
        text=True,
        capture_output=True,
        check=False,
    )


def drifted(result: subprocess.CompletedProcess[str]) -> set[str]:
    prefix = "image manifest drift: "
    return {
        line.removeprefix(prefix).split()[0]
        for line in result.stderr.splitlines()
        if line.startswith(prefix)
    }


def test_a_matching_manifest_passes_from_a_temporary_image_tree(tmp_path: Path) -> None:
    result = check(image_copy(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "image manifest: 5 image tags match build.sh\n"


def test_a_changed_image_file_names_only_that_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "script-harness" / "harness.sh").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"script-harness"}
    assert "images/build.sh script-harness" in result.stderr


def test_a_changed_pin_drifts_every_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "pins.env").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == set(HARNESSES)


def test_a_changed_build_script_drifts_every_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "build.sh").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == set(HARNESSES)


def test_a_missing_manifest_entry_names_the_image_and_fix(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    manifest = images / "manifest.env"
    manifest.write_text(
        "".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("AGY=")
        ),
        encoding="utf-8",
    )

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"agy"}
    assert "images/build.sh agy" in result.stderr


def test_an_orphaned_manifest_entry_is_rejected(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    shutil.rmtree(images / "hermes")

    result = check(images)
    assert result.returncode == 1
    assert "manifest key HERMES has no image directory" in result.stderr
