# 14. PostgreSQL schema outline and migration strategy

## Tables (v0.x)

| Table | Key columns |
|---|---|
| `principals` | id, name, role, token_hash, created_at, disabled_at |
| `policies` | (name, version) PK, document JSONB, created_at, retired_at |
| `tasks` | id ULID PK, external_id, principal_id, project, title, state, contract_version, policy_name, policy_version, created_at, updated_at, closed_at; UNIQUE (principal_id, external_id) |
| `task_contracts` | id, task_id, version, document JSONB, sha256, submitted_at; UNIQUE (task_id, version) |
| `executions` | id, task_id, harness, model, effort, provider, image, policy snapshot JSONB, state, created_at, ended_at |
| `attempts` | id, execution_id, number, state, workspace_path, handle (provider ref), identity_sha256, started_at, ended_at, exit_code, exit_class, timeout_at |
| `workers` | attempt_id PK, state, last_signal_at, last_signal |
| `leases` | id, kind, key, holder, fenced_token BIGINT, expires_at; UNIQUE (kind, key) |
| `heartbeats` | id BIGSERIAL, attempt_id, ts, signal, detail |
| `events` | seq BIGSERIAL PK, ts, kind, task_id, execution_id, attempt_id, principal, verified, payload JSONB |
| `log_chunks` | id BIGSERIAL, attempt_id, stream, offset_start, offset_end, ts, content BYTEA |
| `artifacts` | id, attempt_id, type, path (under artifact root), size, sha256, content_type, created_at |
| `evidence` | id, attempt_id, kind, observed_at, source, verified, payload JSONB, artifact_id |
| `completion_claims` | attempt_id PK, document JSONB, parsed_ok, parse_errors JSONB |
| `gate_results` | id, attempt_id, gate, result, evaluated_at, evidence_ids BIGINT[], detail |
| `acceptance_results` | id, task_id, principal_id, verdict, reasoning, created_at |
| `decisions` | id, task_id, escalation_id, principal_id, verbatim TEXT, resolves, created_at |
| `escalations` | id, task_id, attempt_id, state, question, opened_at, closed_at |
| `wakes` | id, principal_id, task_id, reason, payload JSONB, created_at, delivered_at, acked_at, attempts |
| `supervisor_status` | singleton: holder, last_tick_at, tick_ms, counts JSONB |
| `bootstrap_imports` | id, source_sha256, manifest JSONB, verified_at, committed_at, state |

Indexes: tasks (state), (principal_id, updated_at); events (task_id, seq);
attempts (state); leases (expires_at); wakes (principal_id, acked_at) partial
where acked_at is null.

Triggers: `events` and `task_contracts` reject UPDATE and DELETE. Writes to
`tasks`, `executions`, `attempts` require a `fenced_token` session variable
equal to or newer than the current supervisor lease token, enforced by a
BEFORE trigger, for the supervisor role; the API role is exempt for the
operations it owns (submit, start, cancel, accept, decisions).

## Strategy

- SQLAlchemy 2.x typed ORM with explicit `Mapped[]` annotations; no
  implicit lazy loading in the application layer (repositories return
  domain objects).
- Alembic migrations, one file per change, hand-reviewed (autogenerate as
  a draft only). `crucible-admin migrate` applies; the container entrypoint
  refuses to serve if the head revision is not applied, and `/ready` reports
  it.
- Down migrations required for every revision in v0.x.
- Migrations tested in CI against a fresh database and against a database
  seeded at the previous release's head.
- JSONB documents validated by Pydantic on the way in and out; schema
  versions inside documents, table versions by Alembic. A document schema
  bump that changes stored shape ships with a data migration.
- Backups are the operator's concern (volume snapshot locally; operator
  tooling on the cluster). `crucible-admin export` produces a portable JSON
  bundle for the same reason the bootstrap import exists.
