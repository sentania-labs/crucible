"""Hades #385: Hermes's content search must not lose the search root.

Without ripgrep, Hermes 0.19.0 searches with `grep -rnH --exclude-dir='.*' ... ROOT`, and
GNU grep applies that pattern to ROOT itself, so a search of `.` found nothing. The image
now carries ripgrep, and the wrapper's bootstrap patches the grep fallback to skip only
hidden directories below the root.

The search tests drive the real Hermes the worker image installs, through its own file
operations, with a terminal whose PATH the test chooses; they skip where that Hermes is
not installed (a plain CI runner), and the ripgrep case skips where there is no `rg`. The
version guard is checked against a stand-in Hermes, so it runs everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"
HERMES_PYTHON = Path("/opt/hermes/bin/python")

needs_hermes = pytest.mark.skipif(
    not HERMES_PYTHON.exists(), reason="the worker image's Hermes is not installed here"
)

# Runs under the Hermes venv's Python after the wrapper's patches: one search through
# Hermes's own ShellFileOperations, by a terminal that runs each command in bash with
# PATH set to the directory given, and the result as Hermes reports it.
_SEARCH = r"""
import json
import subprocess

import tools.file_operations as file_operations

checkout, search_path, pattern, root = sys.argv[1:5]


class Terminal:
    cwd = checkout

    def execute(self, command, cwd=None, timeout=None, **_):
        done = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=cwd or self.cwd,
            env={"PATH": search_path},
            capture_output=True,
            text=True,
            check=False,
        )
        return {"output": done.stdout + done.stderr, "returncode": done.returncode}


operations = file_operations.ShellFileOperations(Terminal())
print(json.dumps({
    "rg": operations._has_command("rg"),
    "content": operations.search(pattern, path=root).to_dict(),
    "files_only": operations.search(pattern, path=root, output_mode="files_only").to_dict(),
}))
"""


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path) -> Path:
    """A checkout with the needle in a source file and in a hidden directory."""
    checkout = tmp_path / "checkout"
    (checkout / "docs").mkdir(parents=True)
    (checkout / ".hidden").mkdir()
    (checkout / "docs" / "architecture.md").write_text("see AGENTS here\n", encoding="utf-8")
    (checkout / ".hidden" / "cache.md").write_text("AGENTS cached\n", encoding="utf-8")
    return checkout


def _path_with(tmp_path: Path, programs: list[str]) -> str:
    """A PATH of one directory holding exactly these programs."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for program in programs:
        found = shutil.which(program)
        assert found is not None, program
        (directory / program).symlink_to(found)
    return str(directory)


def _search(checkout: Path, search_path: str, root: str = ".") -> dict[str, object]:
    script = _wrapper().PATCHES + _SEARCH
    result = subprocess.run(
        [str(HERMES_PYTHON), "-P", "-c", script, str(checkout), search_path, "AGENTS", root],
        capture_output=True,
        text=True,
        check=False,
        cwd=checkout,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(checkout.parent)},
    )
    assert result.returncode == 0, result.stderr
    outcome: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return outcome


@needs_hermes
@pytest.mark.parametrize(
    ("root", "found"),
    [(".", "./docs/architecture.md"), ("./", "./docs/architecture.md")],
)
def test_a_relative_root_without_ripgrep_finds_the_file_and_skips_the_hidden_one(
    tmp_path: Path, root: str, found: str
) -> None:
    checkout = _tree(tmp_path)
    outcome = _search(checkout, _path_with(tmp_path, ["grep", "head"]), root)
    assert outcome["rg"] is False
    assert outcome["content"] == {
        "total_count": 1,
        "matches": [{"path": found, "line": 1, "content": "see AGENTS here"}],
    }
    assert outcome["files_only"] == {"total_count": 1, "files": [found]}


@needs_hermes
def test_an_absolute_or_hidden_root_without_ripgrep_is_searched_too(tmp_path: Path) -> None:
    checkout = _tree(tmp_path)
    search_path = _path_with(tmp_path, ["grep", "head"])
    absolute = _search(checkout, search_path, str(checkout))
    assert absolute["files_only"] == {
        "total_count": 1,
        "files": [f"{checkout}/docs/architecture.md"],
    }
    # A hidden directory named as the root is searched, as ripgrep searches it.
    hidden = _search(checkout, search_path, ".hidden")
    assert hidden["files_only"] == {"total_count": 1, "files": [".hidden/cache.md"]}


@needs_hermes
@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter directories without execute bits")
def test_an_unenterable_root_without_ripgrep_surfaces_the_error(tmp_path: Path) -> None:
    checkout = _tree(tmp_path)
    locked = checkout / "locked"
    locked.mkdir()
    (locked / "source.md").write_text("AGENTS\n", encoding="utf-8")
    locked.chmod(0o600)
    try:
        outcome = _search(checkout, _path_with(tmp_path, ["grep", "head"]), "locked")
    finally:
        locked.chmod(0o700)

    content = outcome["content"]
    assert isinstance(content, dict)
    assert content["total_count"] == 0
    assert "Search failed:" in content["error"]
    assert "Permission denied" in content["error"]


@needs_hermes
@pytest.mark.skipif(shutil.which("rg") is None, reason="no ripgrep on this host")
def test_a_relative_root_with_ripgrep_finds_the_file_and_skips_the_hidden_one(
    tmp_path: Path,
) -> None:
    checkout = _tree(tmp_path)
    outcome = _search(checkout, _path_with(tmp_path, ["rg", "head"]))
    assert outcome["rg"] is True
    assert outcome["files_only"] == {"total_count": 1, "files": ["./docs/architecture.md"]}


def _stand_in(tmp_path: Path, version: str, fallback: str) -> Path:
    """A Hermes with this distribution version and this grep fallback source."""
    hermes = tmp_path / "hermes"
    (hermes / "tools").mkdir(parents=True)
    info = hermes / f"hermes_agent-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: hermes-agent\nVersion: {version}\n", encoding="utf-8"
    )
    (hermes / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "tools" / "file_operations.py").write_text(
        f"class ShellFileOperations:\n{fallback}", encoding="utf-8"
    )
    return hermes


_FALLBACK_0_19_0 = """
    def _search_with_grep(self, pattern, path, file_glob, limit, offset, output_mode, context):
        cmd_parts = ["grep", "-rnH"]
        cmd_parts.append("--exclude-dir='.*'")
        cmd_parts.append(self._escape_shell_arg(path))
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])
        cmd = "set -o pipefail; " + " ".join(cmd_parts)
"""


def _patched_import(hermes: Path) -> subprocess.CompletedProcess[str]:
    script = _wrapper().PATCHES + "import tools.file_operations\nprint('imported')\n"
    return subprocess.run(
        [sys.executable, "-P", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=hermes.parent,
        env={"PYTHONPATH": str(hermes)},
    )


def test_the_patches_load_against_hermes_0_19_0(tmp_path: Path) -> None:
    result = _patched_import(_stand_in(tmp_path, "0.19.0", _FALLBACK_0_19_0))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


def test_the_patches_refuse_another_hermes_version(tmp_path: Path) -> None:
    result = _patched_import(_stand_in(tmp_path, "0.20.0", _FALLBACK_0_19_0))
    assert result.returncode == 1
    assert "patches are for hermes-agent 0.19.0, found 0.20.0" in result.stderr
    assert "imported" not in result.stdout


def test_the_patch_refuses_a_grep_fallback_it_was_not_written_for(tmp_path: Path) -> None:
    changed = _FALLBACK_0_19_0.replace("--exclude-dir='.*'", "--exclude-dir='.?*'")
    result = _patched_import(_stand_in(tmp_path, "0.19.0", changed))
    assert result.returncode != 0
    assert "not the one hades #385 patches" in result.stderr
    assert "imported" not in result.stdout


# Runs under the Hermes venv's Python after the wrapper's patches, with a terminal that
# keeps its working directory the way Hermes's own environments do: the command runs in
# bash from that directory, and the shell's `pwd -P` afterwards becomes the new one.
_TRACKED = r"""
import json
import subprocess

import tools.file_operations as file_operations

checkout, search_path = sys.argv[1:3]
MARKER = "__crucible_cwd__"


class Terminal:
    cwd = checkout

    def execute(self, command, cwd=None, timeout=None, **_):
        done = subprocess.run(
            ["/bin/bash", "-c", f'cd -- "$0" || exit 126\n{command}\n__s=$?\n'
             f'printf "\\n{MARKER}%s\\n" "$(pwd -P)"\nexit $__s', cwd or self.cwd],
            env={"PATH": search_path},
            capture_output=True,
            text=True,
            check=False,
        )
        output, _, moved = done.stdout.rpartition(f"\n{MARKER}")
        if moved.strip():
            self.cwd = moved.strip()
        return {"output": output + done.stderr, "returncode": done.returncode}


terminal = Terminal()
operations = file_operations.ShellFileOperations(terminal)
searches = []
for root in ("docs", "docs", "."):
    searches.append(operations.search("AGENTS", path=root, output_mode="files_only").to_dict())
print(json.dumps({"cwd": terminal.cwd, "searches": searches}))
"""


@needs_hermes
def test_a_search_without_ripgrep_leaves_the_working_directory_where_it_was(
    tmp_path: Path,
) -> None:
    """Hermes records `pwd -P` after each command as the session's directory, so a cd
    at the top level of the fallback would move the agent into every root it searched."""
    checkout = _tree(tmp_path)
    script = _wrapper().PATCHES + _TRACKED
    result = subprocess.run(
        [
            str(HERMES_PYTHON),
            "-P",
            "-c",
            script,
            str(checkout.resolve()),
            _path_with(tmp_path, ["grep", "head"]),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=checkout,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(checkout.parent)},
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["cwd"] == str(checkout.resolve())
    assert outcome["searches"] == [
        {"total_count": 1, "files": ["docs/architecture.md"]},
        {"total_count": 1, "files": ["docs/architecture.md"]},
        {"total_count": 1, "files": ["./docs/architecture.md"]},
    ]


def test_a_grep_fallback_of_another_shape_stops_the_attempt_before_hermes_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Hermes's tool discovery swallows an import error, so the wrapper checks first."""
    changed = _FALLBACK_0_19_0.replace("--exclude-dir='.*'", "--exclude-dir='.?*'")
    hermes = _stand_in(tmp_path, "0.19.0", changed)
    # Were Hermes started, this stand-in entry point would say so.
    (hermes / "hermes_cli").mkdir()
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text(
        "def main():\n    print('hermes started')\n    return 0\n", encoding="utf-8"
    )
    wrapper = _wrapper()
    monkeypatch.setattr(wrapper, "HERMES_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(hermes))
    monkeypatch.setenv("CRUCIBLE_HERMES_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", ["crucible-hermes", "-z", "prompt"])
    monkeypatch.chdir(tmp_path)

    assert wrapper.main() == 2
    captured = capfd.readouterr()
    assert "hermes started" not in captured.out
    assert "not the one hades #385 patches" in captured.err
    assert wrapper.PREFLIGHT_FAILED in captured.err


def test_the_preflight_passes_against_hermes_0_19_0_and_hermes_then_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    hermes = _stand_in(tmp_path, "0.19.0", _FALLBACK_0_19_0)
    (hermes / "hermes_cli").mkdir()
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text(
        "def main():\n    print('hermes started')\n    return 0\n", encoding="utf-8"
    )
    wrapper = _wrapper()
    monkeypatch.setattr(wrapper, "HERMES_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(hermes))
    monkeypatch.setenv("CRUCIBLE_HERMES_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", ["crucible-hermes", "-z", "prompt"])
    monkeypatch.chdir(tmp_path)

    assert wrapper.main() == 0
    assert "hermes started" in capfd.readouterr().out


def test_the_patched_hermes_version_is_the_one_the_image_installs() -> None:
    """A Dockerfile bump without the wrapper would build green and then refuse every
    Hermes task; every HARNESS_HERMES_VERSION the Dockerfile declares must match."""
    dockerfile = (WRAPPER.parent / "Dockerfile").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[1].strip()
        for line in dockerfile.splitlines()
        if line.startswith("ARG HARNESS_HERMES_VERSION=")
    }
    assert declared == {_wrapper().HERMES_VERSION}
