# ADR 0006: SQLite bootstrap ledger for Foundry until handoff

Status: accepted by the operator, 2026-09-16.

## Decision

Foundry's interim ledger moves from YAML plus JSONL to a single SQLite
database with numbered migrations, transactions, uniqueness, and a
verified import path. The YAML files are retained read-only as evidence.
On Crucible readiness, the ledger is exported as `BootstrapExportV1`,
imported, verified by count and content hash, committed as authoritative,
and the SQLite file is marked migrated and kept read-only for a retention
period. There is never more than one writable ledger.

## Consequences

Foundry's bootstrap tooling gains a small CLI. The handoff is a tested API
path rather than a manual copy. After handoff, Foundry is replaceable by
any client holding the token.
