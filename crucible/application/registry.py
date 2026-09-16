"""Registered providers and harnesses for C1. The fake provider supports every harness."""

from __future__ import annotations

from crucible.contracts.task_contract import HarnessName, ProviderName

REGISTERED_HARNESSES: frozenset[str] = frozenset(h.value for h in HarnessName)

# Only the fake provider exists in C1; docker is C3 (20).
REGISTERED_PROVIDERS: dict[str, frozenset[str]] = {
    ProviderName.FAKE.value: REGISTERED_HARNESSES,
}
