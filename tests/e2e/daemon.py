"""Out-of-band daemon control for the e2e tier (18).

The tests need a daemon of their own to stand up PostgreSQL and the two proxies, and
to do the things a test does behind Crucible's back: remove a worker container to
force `lost`, plant a labelled container to force an orphan. That is the only reason
this module exists, and it is deliberately not the path Crucible uses: Crucible only
ever talks to the socket proxy.

`CRUCIBLE_E2E_DOCKER` names the docker command. On the reference workstation it is a
sudo wrapper for the `crucible` service user's rootless daemon (S9); in CI it is
plain `docker` against the runner's own daemon.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def docker_argv() -> list[str]:
    return shlex.split(os.environ.get("CRUCIBLE_E2E_DOCKER", "docker"))


def run(*args: str, check: bool = True, timeout: float = 300.0) -> str:
    completed = subprocess.run(
        [*docker_argv(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def inspect(reference: str) -> dict[str, Any] | None:
    out = run("inspect", reference, check=False)
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed[0] if parsed else None


def rm(*names: str) -> None:
    for name in names:
        run("rm", "-f", "-v", name, check=False)


def container_ids(label: str) -> list[str]:
    out = run("ps", "-a", "--filter", f"label={label}", "--format", "{{.ID}}")
    return [line for line in out.split() if line]


def wait_for_port(container: str, port: int, *, attempts: int = 60) -> None:
    """The container is up when it answers on its own port from inside itself."""
    for _ in range(attempts):
        state = inspect(container)
        if state and state.get("State", {}).get("Running"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{container} never started")


MANIFEST = Path(__file__).resolve().parents[2] / "images" / "manifest.env"


def manifest_pins() -> dict[str, str]:
    """The declared image per harness from images/manifest.env, written by build.sh (13).

    Every reproducible image carries the same SOURCE_DATE_EPOCH creation time, so no
    tier picks an image by "newest": two tags of one harness tie, and the choice would
    be arbitrary (found by Foundry on 2026-09-17 at 07:26 CDT)."""
    pins: dict[str, str] = {}
    if not MANIFEST.is_file():
        return pins
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line or line.endswith("_DIGEST"):
            continue
        key, value = line.split("=", 1)
        if key.endswith("_DIGEST"):
            continue
        pins[key.lower().replace("_", "-") if key == "SCRIPT_HARNESS" else key.lower()] = value
    return pins


def image_tag(prefix: str, *, harness: str | None = None) -> str:
    """The image for a harness: the manifest's pin when it names one that the daemon
    has; otherwise the one local tag with the prefix. More than one match and no pin is
    a refusal, never a guess."""
    out = run("images", "--format", "{{.Repository}}:{{.Tag}}")
    tags = [t for t in out.split() if t.startswith(prefix)]
    name = harness or prefix.removeprefix("crucible-worker:").rstrip("-")
    pinned = manifest_pins().get(name)
    if pinned:
        if pinned in tags:
            return pinned
        raise RuntimeError(
            f"images/manifest.env pins {pinned} for {name} but the daemon has no such tag; "
            "build it with images/build.sh"
        )
    if not tags:
        raise RuntimeError(f"no image tagged {prefix}*; build it with `make e2e-image` (18)")
    if len(tags) > 1:
        raise RuntimeError(
            f"{len(tags)} images match {prefix}* and images/manifest.env pins none: {tags}"
        )
    return tags[0]


def logs(container: str, *, tail: int = 200) -> str:
    return run("logs", "--tail", str(tail), container, check=False)


def free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ensure_network(name: str, *, internal: bool, subnet: str | None = None) -> None:
    # `docker network inspect` on a missing network prints `[]` and exits non-zero, so
    # the emptiness of the parsed list is the test, never the emptiness of the output.
    out = run("network", "inspect", name, check=False)
    try:
        if json.loads(out or "[]"):
            return
    except json.JSONDecodeError:
        pass
    args = ["network", "create"]
    if internal:
        args.append("--internal")
    if subnet:
        args += ["--subnet", subnet]
    run(*args, name)


def remove_network(name: str) -> None:
    run("network", "rm", name, check=False)


def run_detached(name: str, args: Sequence[str]) -> str:
    rm(name)
    return run("run", "-d", "--name", name, *args).strip()
