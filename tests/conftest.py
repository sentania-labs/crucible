"""Suite-wide pytest hooks."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

pytest_plugins = ["tests.integration.postgres"]

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def cpu_limit_workers(cpu_max: str | None, cpus: int) -> int:
    """How many xdist workers `-n auto` starts: the CPUs this process may use, capped by
    its cgroup's CPU quota when it has one (hades #184).

    In a Kubernetes Pod `os.cpu_count()` and the affinity mask both report the node's
    CPUs, while the Pod may use only its CPU limit. `make test-unit` in a worker or
    verifier Pod would start one worker per node CPU, each importing the whole package,
    against a limit of two. `cpu.max` reads `max 100000` without a quota and
    `200000 100000` for two CPUs."""
    workers = max(1, cpus)
    if not cpu_max:
        return workers
    quota, _, period = cpu_max.strip().partition(" ")
    if quota == "max" or not quota.isdigit() or not period.isdigit() or int(period) == 0:
        return workers
    return max(1, min(workers, math.ceil(int(quota) / int(period))))


# Optional: the hook exists only while pytest-xdist is loaded (`-p no:xdist` runs too).
@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config: object) -> int | None:
    # xdist's own override still wins: returning None hands the choice back to it.
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    try:
        cpu_max: str | None = CGROUP_CPU_MAX.read_text(encoding="utf-8")
    except OSError:
        cpu_max = None
    return cpu_limit_workers(cpu_max, len(os.sched_getaffinity(0)))
