"""The release notes carry the published image digests, read back from the registry
with `docker buildx imagetools inspect` (FDY-0088, FDY-0096). A stand-in `docker`
answers from a table, so these run without a registry."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "release_notes.py"

SERVICE = "ghcr.io/sentania-labs/crucible"
WORKER = "ghcr.io/sentania-labs/crucible-worker"
A, B, C, D = ("sha256:" + ch * 64 for ch in "abcd")


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


rn = _module()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake `docker` whose `buildx imagetools inspect <ref>` answers from `table`: a
    digest, None for "not found", or any other string for an error it prints. Every
    assignment is written through to the file the fake reads."""
    state = tmp_path / "registry.json"
    fake = tmp_path / "docker"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"table = json.load(open({str(state)!r}))\n"
        "ref = sys.argv[4]\n"
        "answer = table.get(ref)\n"
        "if answer is None:\n"
        "    print(f'ERROR: {ref}: not found', file=sys.stderr); sys.exit(1)\n"
        "if not answer.startswith('sha256:'):\n"
        "    print(answer, file=sys.stderr); sys.exit(1)\n"
        "print(json.dumps(answer))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("DOCKER", str(fake))

    class Table(dict[str, Any]):
        def __setitem__(self, key: str, value: Any) -> None:
            super().__setitem__(key, value)
            state.write_text(json.dumps(self))

    state.write_text("{}")
    return Table()


def _publish(registry: dict[str, Any], latest: str | None) -> None:
    registry[f"{SERVICE}:0.5.3"] = A
    registry[f"{WORKER}:0.5.3"] = B
    registry[f"{WORKER}:script-harness-0.5.3"] = C
    if latest is not None:
        registry[f"{WORKER}:latest"] = latest


def _run(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = rn.main(["--service-image", f"{SERVICE}:0.5.3", "--worker-repository", WORKER])
    out, err = capsys.readouterr()
    return code, out, err


def test_notes_name_all_three_images_by_their_read_back_digest(
    registry: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(registry, latest=B)
    code, out, _ = _run(capsys)
    assert code == 0
    assert out.startswith("## Published image digests\n")
    assert f"- `{SERVICE}:0.5.3@{A}`" in out
    assert f"- `{WORKER}:0.5.3@{B}`" in out
    assert f"- `{WORKER}:script-harness-0.5.3@{C}`" in out
    assert "left alone" not in out


def test_notes_say_plainly_when_latest_did_not_move(
    registry: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(registry, latest=D)
    code, out, _ = _run(capsys)
    assert code == 0
    assert f"`{WORKER}:latest` was left alone: 0.5.3 is not the highest" in out


def test_an_unpublished_worker_image_fails_rather_than_printing_a_digest(
    registry: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(registry, latest=B)
    registry[f"{WORKER}:script-harness-0.5.3"] = None
    code, out, err = _run(capsys)
    assert code == 1
    assert out == ""
    assert "script-harness-0.5.3 is not published" in err


def test_a_registry_error_is_never_read_as_absent(
    registry: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(registry, latest="ERROR: failed to do request: 429 Too Many Requests")
    code, out, err = _run(capsys)
    assert code == 1
    assert out == ""
    assert "429" in err
