"""`tools/release/push_worker_images.sh` pushes the worker images with `docker push`
and never over an existing version tag (FDY-0096). A stand-in `docker` plays both the
daemon and the registry, and logs every call, so the never-overwrite paths run without
either."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "push_worker_images.sh"
REPO = "registry.example/crucible-worker"
WORKER_LOCAL = "crucible-worker:20260916-aaaaaaaaaaaa"
HARNESS_LOCAL = "crucible-worker:script-harness-1.0.0-bbbbbbbbbbbb"
WORKER_INPUTS = "sha256:" + "a" * 64
HARNESS_INPUTS = "sha256:" + "b" * 64

FAKE = """\
import json, sys
state = json.load(open(sys.argv[1]))
args = sys.argv[2:]
with open(state["log"], "a") as log:
    log.write(" ".join(args) + "\\n")
if args[:2] == ["image", "inspect"]:
    print(state["local"][args[-1]])
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    ref = args[3]
    answer = state["registry"].get(ref)
    if answer is None:
        print("WARNING: a line before the answer", file=sys.stderr)
        print(f"ERROR: {ref}: not found", file=sys.stderr); sys.exit(1)
    if answer.startswith("ERROR"):
        print(answer, file=sys.stderr); sys.exit(1)
    print(json.dumps({"crucible.build_inputs": answer}))
"""


def _run(
    tmp_path: Path, registry: dict[str, Any]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "calls.log"
    log.write_text("")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "log": str(log),
                "local": {WORKER_LOCAL: WORKER_INPUTS, HARNESS_LOCAL: HARNESS_INPUTS},
                "registry": registry,
            }
        )
    )
    fake = tmp_path / "fake_docker.py"
    fake.write_text(FAKE)
    manifest = tmp_path / "manifest.env"
    manifest.write_text(
        f"SCRIPT_HARNESS={HARNESS_LOCAL}\nSCRIPT_HARNESS_DIGEST=sha256:{'c' * 64}\n"
        f"WORKER={WORKER_LOCAL}\nWORKER_DIGEST=sha256:{'d' * 64}\n"
    )
    env = dict(os.environ)
    env.update(
        DOCKER=f"{sys.executable} {fake} {state}",
        MANIFEST=str(manifest),
        VERSION="0.5.3",
        WORKER_REGISTRY=REPO,
    )
    result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
    calls = [line for line in log.read_text().splitlines() if line.split()[0] in ("tag", "push")]
    return result, calls


def test_absent_version_tags_are_pushed(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"tag {WORKER_LOCAL} {REPO}:0.5.3",
        f"push {REPO}:0.5.3",
        f"tag {HARNESS_LOCAL} {REPO}:script-harness-0.5.3",
        f"push {REPO}:script-harness-0.5.3",
    ]


def test_a_half_published_release_rerun_pushes_only_what_is_missing(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {f"{REPO}:0.5.3": WORKER_INPUTS})
    assert result.returncode == 0, result.stderr
    assert "already published from these build inputs" in result.stdout
    assert calls == [
        f"tag {HARNESS_LOCAL} {REPO}:script-harness-0.5.3",
        f"push {REPO}:script-harness-0.5.3",
    ]


def test_a_version_tag_from_other_inputs_is_never_overwritten(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {f"{REPO}:0.5.3": "sha256:" + "e" * 64})
    assert result.returncode == 1
    assert "never overwritten" in result.stderr
    assert calls == []


def test_a_registry_error_is_never_read_as_absent(tmp_path: Path) -> None:
    denied = "ERROR: failed to authorize: 403 Forbidden"
    result, calls = _run(tmp_path, {f"{REPO}:0.5.3": denied})
    assert result.returncode == 1
    assert "cannot tell whether" in result.stderr
    assert "403 Forbidden" in result.stderr
    assert calls == []
