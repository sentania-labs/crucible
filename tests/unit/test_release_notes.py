"""The release notes carry the published image digests, read back from the registry
(FDY-0088). `render` is the pure formatting the CLI hands its registry read-back to; it
is tested here on its own, without a network, the same way `worker_images.py`'s pure
helpers are."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "release_notes.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


rn = _module()


def test_render_names_both_images_by_their_read_back_digest() -> None:
    text = rn.render(
        "ghcr.io/sentania-labs/crucible:0.5.0",
        "sha256:" + "a" * 64,
        "ghcr.io/sentania-labs/crucible-worker:20260916-c6c15cef2f5c",
        "sha256:" + "b" * 64,
    )
    assert "ghcr.io/sentania-labs/crucible:0.5.0@sha256:" + "a" * 64 in text
    assert "ghcr.io/sentania-labs/crucible-worker:20260916-c6c15cef2f5c@sha256:" + "b" * 64 in text


def test_worker_image_reads_the_declared_worker_entry(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.env"
    manifest.write_text(
        "SCRIPT_HARNESS=crucible-worker:script-harness-1.0.0\n"
        "SCRIPT_HARNESS_DIGEST=sha256:" + "c" * 64 + "\n"
        "WORKER=crucible-worker:20260916-c6c15cef2f5c\n"
        "WORKER_DIGEST=sha256:" + "d" * 64 + "\n"
    )
    tag, digest = rn.worker_image(manifest)
    assert tag == "20260916-c6c15cef2f5c"
    assert digest == "sha256:" + "d" * 64
