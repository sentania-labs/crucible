"""Tests for the e2e-kind readiness gate in cluster.sh and e2e-kind.sh.

No cluster is available in the unit tier, so these tests use static analysis
(grep / bash -n) to verify the scripts are syntactically valid and contain
the required function, exit code, and message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def cluster_sh() -> Path:
    return ROOT / "tools" / "kind" / "cluster.sh"


@pytest.fixture()
def e2e_kind_sh() -> Path:
    return ROOT / "tools" / "kind" / "e2e-kind.sh"


def test_cluster_sh_syntax(cluster_sh: Path) -> None:
    """cluster.sh parses without syntax errors."""
    result = subprocess.run(
        ["bash", "-n", str(cluster_sh)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_e2e_kind_sh_syntax(e2e_kind_sh: Path) -> None:
    """e2e-kind.sh parses without syntax errors."""
    result = subprocess.run(
        ["bash", "-n", str(e2e_kind_sh)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cluster_sh_has_crucible_kind_wait_ready(cluster_sh: Path) -> None:
    """cluster.sh defines the crucible_kind_wait_ready function."""
    content = cluster_sh.read_text()
    assert "crucible_kind_wait_ready()" in content


def test_cluster_sh_has_3_minute_deadline(cluster_sh: Path) -> None:
    """crucible_kind_wait_ready uses a 3-minute (180 s) deadline."""
    content = cluster_sh.read_text()
    assert "180" in content  # 3 minutes = 180 seconds
    assert "deadline" in content.lower()


def test_cluster_sh_has_four_checks(cluster_sh: Path) -> None:
    """The function contains all four readiness checks."""
    content = cluster_sh.read_text()
    assert "nodes not Ready" in content
    assert "calico" in content.lower()
    assert "coredns" in content.lower()
    assert "api server" in content.lower() or "api_ip" in content or "readiness pod" in content


def test_e2e_kind_sh_calls_wait_ready(e2e_kind_sh: Path) -> None:
    """e2e-kind.sh invokes the readiness gate."""
    content = e2e_kind_sh.read_text()
    assert "crucible_kind_wait_ready" in content


def test_e2e_kind_sh_preloads_worker_image(e2e_kind_sh: Path) -> None:
    """e2e-kind.sh loads the worker image via kind."""
    content = e2e_kind_sh.read_text()
    assert "kind load docker-image" in content


def test_e2e_kind_sh_has_infrastructure_exit_code(e2e_kind_sh: Path) -> None:
    """On both gate attempts failing the script exits with code 75."""
    content = e2e_kind_sh.read_text()
    assert "exit 75" in content


def test_e2e_kind_sh_has_infrastructure_message(e2e_kind_sh: Path) -> None:
    """The failure message identifies the problem as infrastructure."""
    content = e2e_kind_sh.read_text()
    assert "kind cluster not healthy (infrastructure)" in content
