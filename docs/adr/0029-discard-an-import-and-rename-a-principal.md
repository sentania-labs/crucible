# ADR 0029: Discard a verified import, skip native tasks, rename a principal

Status: accepted. The operator's direction of 2026-09-29: "discard and import fresh.
Also - we should "name" the account "hades" since that will eventually be the principal
agent's identity." Amends the handoff procedure of spec 15 and the operations of spec 25.

## Context

The lab held a `verified` import from a ledger exported on 2026-09-28. By the next
afternoon the ledger had moved on, so the handoff needed a fresh export, and it was
refused: every task of the old import already held its external id for the owner, and
nothing could withdraw a verified import, only commit it. Two tasks, FDY-0137 and
FDY-0144, also refused the bundle for a different reason: they were Hades's own tasks,
run through the API, that the ledger had recorded too.

The orchestrator's principal was named `foundry`. The operator wants it named `hades`,
the identity the principal agent will carry, without re-issuing its token or moving its
tasks.

## Decision

1. **A verified import can be discarded.** `POST /import/bootstrap/{id}/discard`, admin,
   reason required; `crucible admin bootstrap discard ID`; a Discard button beside
   Commit on the Bootstrap page. Only a `verified` import: an `authoritative` one is the
   ledger and is never withdrawn (ADR 0006).
2. **A discard retires; it deletes nothing.** Events are append-only (10), and every
   imported task carries events. So the import's tasks move to a disabled observer
   principal of their own, `discarded-import-<import id>`, which frees their external
   ids for the owner. A task still open is cancelled, its synthetic execution cancelled
   and its unsupervised attempt ended as failed with `bootstrap_import_discarded`. Each
   task records `bootstrap_import_discarded`, and the import's state becomes `discarded`
   with who, when and why in its manifest. A discarded import is never replayed: the
   same bundle imports afresh.
3. **A native task is skipped, not refused.** A bundle task whose external id the owner
   already has, on a task no import wrote, is Hades's own record. The import skips it and
   its events and lists it under `skipped_existing`; `counts` are what was written, with
   `skipped_tasks` and `skipped_events` beside them. A task another import wrote still
   refuses the bundle, because two imports never share a task.
4. **A principal can be renamed.** `POST /admin/tokens/{principal_id}/rename` with a
   `name`, admin, reason required; `crucible admin token rename ID NAME`; a Rename form
   on the Tokens page. Tasks, tokens and grants hold the principal's id, so they follow
   it and the token keeps working. Events keep the name they were written with; they
   are history. A taken name, a reserved one (`crucible`, `worker:...`,
   `first-run-admin...`) and a rename of the first-run principal are refused. Audited as
   `principal_renamed` with the old and new names.

## Consequences

- Migration 0032 adds the two event kinds and `discarded` to the import states. Its
  downgrade refuses while a discarded import exists, since 0031 has no state for it.
- A discarded import's tasks stay readable by id and in the audit trail, under their
  retired principal, and no longer appear among the owner's tasks.
