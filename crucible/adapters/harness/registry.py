"""The four adapters as one registry (07)."""

from __future__ import annotations

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.harnesses import HarnessRegistry
from crucible.ports.harness import HarnessAdapter


def default_adapters() -> tuple[HarnessAdapter, ...]:
    return (ClaudeCodeAdapter(), CodexAdapter(), AgyAdapter(), ScriptHarnessAdapter())


def default_registry() -> HarnessRegistry:
    return HarnessRegistry(default_adapters())
