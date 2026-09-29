"""`-n auto` follows the cgroup CPU quota, not the node's CPUs (hades #184)."""

from __future__ import annotations

import pytest

from tests.conftest import cpu_limit_workers


@pytest.mark.parametrize(
    ("cpu_max", "cpus", "workers"),
    [
        (None, 16, 16),
        ("max 100000\n", 16, 16),
        ("200000 100000\n", 16, 2),
        ("150000 100000\n", 16, 2),
        ("50000 100000\n", 16, 1),
        ("800000 100000\n", 4, 4),
        ("garbage", 8, 8),
        ("100000 0", 8, 8),
        (None, 0, 1),
    ],
)
def test_cpu_limit_workers(cpu_max: str | None, cpus: int, workers: int) -> None:
    assert cpu_limit_workers(cpu_max, cpus) == workers
