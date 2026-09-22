"""A launch outside the adapter's tested range is refused, never warned about (07, 13)."""

from __future__ import annotations

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.harnesses import check_image_version as _check
from crucible.application.harnesses import egress_allowlist

REGISTRY = default_registry()


def check_image_version(harness: str, labels: dict[str, str]):  # type: ignore[no-untyped-def]
    return _check(REGISTRY, harness, labels)


def labels(harness: str, version: str) -> dict[str, str]:
    return {"crucible.harness": harness, "crucible.harness_version": version}


def test_a_version_inside_the_range_is_accepted() -> None:
    check = check_image_version("codex", labels("codex", "0.153.4"))
    assert check.ok and check.installed == "0.153.4"
    assert check.supported == ">=0.153.0,<0.154.0"


def test_claude_code_version_that_predates_agents_md_is_refused() -> None:
    check = check_image_version("claude_code", labels("claude_code", "2.1.273"))
    assert not check.ok
    assert check.supported == ">=2.1.277,<2.2.0"


def test_claude_code_agents_md_floor_is_accepted() -> None:
    check = check_image_version("claude_code", labels("claude_code", "2.1.277"))
    assert check.ok


@pytest.mark.parametrize("version", ["0.152.9", "0.154.0", "1.0.0"])
def test_a_version_outside_the_range_is_refused(version: str) -> None:
    check = check_image_version("codex", labels("codex", version))
    assert not check.ok
    assert "outside the tested range" in check.detail


def test_an_image_with_no_version_label_is_refused() -> None:
    check = check_image_version("codex", {"crucible.harness": "codex"})
    assert not check.ok and "no crucible.harness_version" in check.detail


def test_an_image_for_another_harness_is_refused() -> None:
    check = check_image_version("codex", labels("claude_code", "2.1.273"))
    assert not check.ok and "declares harness" in check.detail


def test_an_unknown_harness_is_refused() -> None:
    check = check_image_version("nonesuch", labels("nonesuch", "1.0.0"))
    assert not check.ok and "no adapter declares" in check.detail


def test_an_unparsable_version_is_refused() -> None:
    assert not check_image_version("codex", labels("codex", "latest")).ok


def test_the_script_harness_is_declared_for_the_e2e_tier() -> None:
    adapter = REGISTRY.require("script-harness")
    assert adapter.supported_versions.supports("1.0.0")
    # 18: no model, so no endpoint it must reach.
    assert adapter.capabilities().endpoints == ()


def test_the_allowlist_is_the_union_of_policy_and_adapter_endpoints() -> None:
    """13: the worker allowlist is the policy's list plus the adapter's endpoints (S6)."""
    hosts = egress_allowlist(REGISTRY, "claude_code", ["github.com", "pypi.org"], [])
    assert hosts == ("api.anthropic.com", "github.com", "pypi.org")
    # A local model endpoint the routing policy names is added for that attempt (05b).
    hosts = egress_allowlist(REGISTRY, "codex", ["github.com"], ["spark.example.internal"])
    assert "spark.example.internal" in hosts and "api.openai.com" in hosts


def test_an_image_with_no_harness_label_is_refused_before_launch() -> None:
    """13 (review C1): an image that does not say which harness it carries never runs
    with any harness's credential, whatever its version label says."""
    check = check_image_version("codex", {"crucible.harness_version": "0.153.4"})
    assert not check.ok and "no crucible.harness label" in check.detail
    check = check_image_version(
        "codex", {"crucible.harness": "", "crucible.harness_version": "0.153.4"}
    )
    assert not check.ok and "no crucible.harness label" in check.detail
    assert not check_image_version("codex", labels("claude_code", "0.153.4")).ok
    assert check_image_version("codex", labels("codex", "0.153.4")).ok
