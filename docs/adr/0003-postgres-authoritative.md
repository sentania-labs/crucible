# ADR 0003: PostgreSQL is the only authoritative state

Status: proposed, 2026-09-16.

## Decision

All lifecycle, event, evidence, decision, and lease state lives in
PostgreSQL. Artifacts and logs live on a filesystem or object store and are
referenced by row with a hash. No file, board, chat thread, issue tracker,
or orchestrator memory is authoritative. Lease fencing is enforced by
database triggers so a stale supervisor cannot write.

## Consequences

Every interface is a client. Backups are one volume. The bootstrap SQLite
ledger is a one-way import, not a peer. Kubernetes deployment needs a
PostgreSQL instance, which the lab already operates.
