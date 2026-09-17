"""Registered providers and the harnesses each one runs (08, 20)."""

from __future__ import annotations

from crucible.contracts.task_contract import HarnessName, ProviderName

REGISTERED_HARNESSES: frozenset[str] = frozenset(h.value for h in HarnessName)

REGISTERED_PROVIDERS: dict[str, frozenset[str]] = {
    # The fake provider is deterministic and supports every harness name (08).
    ProviderName.FAKE.value: REGISTERED_HARNESSES,
    # The Docker provider arrived in C3. The three real harnesses go live in C5; what
    # C3 runs on it is the script harness of 18, which needs no model and no credential.
    ProviderName.DOCKER.value: REGISTERED_HARNESSES,
}
