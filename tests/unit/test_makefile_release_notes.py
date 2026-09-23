"""`make release-notes > release-notes.md` in the release workflow makes the recipe's
stdout a GitHub release body (FDY-0089 review round). An unsilenced recipe line would
put the command itself, not the notes' first Markdown line, at the top of that file.

The recipe's own program is not exercised here (`test_release_notes.py` already covers
`render`); a stand-in `python3` is put on PATH so this test is about Make's own command
echo, not the registry read-back."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def test_release_notes_recipe_writes_only_the_notes_to_stdout(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '## Published image digests'\n"
        "printf '%s\\n' ''\n"
        "printf '%s\\n' '- `example/service:1.0.0@sha256:aaaa`'\n"
        "printf '%s\\n' '- `example/worker:1.0.0@sha256:bbbb`'\n",
        encoding="utf-8",
    )
    fake_python3.chmod(fake_python3.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["CRUCIBLE_IMAGE"] = "example/service:1.0.0"
    # Running under `make test-unit` makes this a sub-make: MAKEFLAGS would otherwise
    # carry '-w' down, and the "Entering directory" line it prints goes to stdout too.
    env.pop("MAKEFLAGS", None)
    env.pop("MAKELEVEL", None)

    result = subprocess.run(
        ["make", "release-notes"],
        cwd=REPOSITORY,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == "## Published image digests"
    assert "release_notes.py" not in result.stdout
