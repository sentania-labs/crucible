# ADR 0001: Modular monolith in typed Python

Status: proposed, 2026-09-16.

## Context

Crucible must be deterministic, testable without infrastructure, portable
from Docker Compose to Kubernetes, and small enough for one operator to run
and one worker agent to extend. The operator specified Python.

## Decision

One deployable service, Python 3.12, `mypy --strict`, with a strict inward
dependency rule: domain, then application and ports, then adapters.
Enforced by import-linter in CI. Processes (`api`, `supervisor`) are roles
of the same image, not separate codebases.

## Consequences

Lifecycle logic is unit-testable with no database or Docker. Swapping the
execution provider or persistence adapter does not touch the domain.
Splitting into services later is a deployment change, not a rewrite. The
cost is discipline: a worker adding a feature must place it in the right
layer, and CI rejects shortcuts.
