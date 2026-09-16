# 15. Foundry bootstrap ledger and authority handoff

## Why SQLite for the bootstrap period

Between now and Crucible's readiness gate, Foundry runs several workers,
switches harnesses, and records failures. Flat YAML gave no transactions, no
uniqueness, no ordering guarantee beyond file position, and made two
concurrent writers unsafe. SQLite gives all four with zero services, and its
records map one-to-one onto Crucible's import contract. Decision: adopt
(ADR 0006 in this repository, mirrored in Foundry's repo).

## Foundry ledger design (implemented in Foundry's repo as `foundry-ledger`)

- One file, `ledger.sqlite`, in Foundry's private state directory, outside
  any public repository.
- Numbered SQL migrations applied by the tool, recorded in
  `schema_migrations`.
- Tables: `tasks` (the same field set as the YAML records, `id` PK, JSON
  columns for structured fields, state CHECK against the lifecycle list),
  `events` (`seq` autoincrement preserving file order, ts, task, event, who,
  detail), `state_transitions` (derived), `import_manifest` (source path,
  sha256, count, imported_at).
- Every write is one transaction. `verify` recounts and rehashes against
  the manifest. `export` writes the YAML shape back for humans.
- The original YAML and JSONL files are retained read-only as bootstrap
  evidence and are never written again after the migration commit.

## Import contract (BootstrapExportV1)

Produced by `foundry-ledger export --format crucible`:

```json
{
  "schema_version": "1.0",
  "source": { "tool": "foundry-ledger", "db_sha256": "...", "exported_at": "..." },
  "tasks": [ { "external_id": "FDY-0001", "title": "...", "state": "done",
               "created": "...", "updated": "...", "contract": {...},
               "refs": {...}, "evidence": [...], "last_report": "...",
               "blockers": [...], "decisions_pending": [...] } ],
  "events": [ { "seq": 1, "ts": "...", "task": "FDY-0001", "event": "...",
                "who": "operator", "detail": "..." } ],
  "counts": { "tasks": 8, "events": 25 },
  "content_sha256": "sha256 over canonical JSON of tasks and events"
}
```

## Handoff procedure (deterministic, each step an event)

1. Foundry runs `foundry-ledger export --format crucible` and POSTs the
   bundle to `/v1/import/bootstrap` as admin.
2. Crucible validates `schema_version`, recomputes `content_sha256`,
   checks `counts` against array lengths, checks `external_id` uniqueness,
   checks each state is mappable to a Crucible task state
   (`done` to `closed`, `accepted` to `accepted`, `reported` to
   `awaiting_acceptance`, `running` and `dispatched` to `running` with a
   synthetic `bootstrap` execution and attempt marked `unsupervised`,
   `proposed` to `submitted`, `blocked` to `blocked`, `abandoned` to
   `cancelled`), and checks event order is strictly increasing by `seq`.
   Any failure returns the full problem list and stores nothing.
3. Records are written in one transaction under a `bootstrap_imports` row in
   state `verified`. `external_id` is preserved on every task; events get
   new global `seq` values but keep their original `seq` in the payload.
   Timestamps are normalized to UTC on import; the original string is kept in the event payload.
4. The response is a verification report: counts written, per-state map,
   content hash, and a diff of any field that could not be carried.
5. Foundry (or the operator) POSTs `/commit`. The import row becomes
   `authoritative`; an event on every imported task records the handoff.
6. Foundry runs `foundry-ledger mark-migrated --crucible-import <id>`. The
   SQLite file gets a `migrated` row and its file mode is set read-only.
   Every subsequent `foundry-ledger` write command refuses with the import
   ID in its message.
7. The SQLite file is retained read-only for the retention period (default
   180 days, configurable), then archived with the YAML evidence.
8. All operational writes go through `/v1`. Foundry's start-of-session
   procedure changes to: `GET /v1/tasks?state=...` and `GET /v1/wakes`.
9. Foundry keeps no other ledger. Its private state directory retains only
   configuration, its identity, and the read-only archive.

After step 6, Foundry is a client. Any harness, or a person with the token,
can carry on from Crucible's state alone.
