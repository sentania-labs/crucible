# 14. PostgreSQL schema outline and migration strategy

## Tables (v0.x)

| Table | Key columns |
|---|---|
| `principals` | id, name, role, token_hash, webhook_url (nullable; one configured target may serve all principals in v0.x), webhook_secret_ref, created_at, disabled_at |
| `policies` | (name, version) PK, document JSONB, created_at, retired_at |
| `tasks` | id ULID PK, external_id, principal_id, project, title, state, contract_version, policy_name, policy_version, head_sha (current collected head), created_at, updated_at, closed_at; UNIQUE (principal_id, external_id) |
| `task_contracts` | id, task_id, version, document JSONB, sha256, submitted_at; UNIQUE (task_id, version) |
| `executions` | id, task_id, role (implement, correct, review), contract_version, harness, model, effort, provider, image, policy snapshot JSONB, state, created_at, ended_at |
| `attempts` | id, execution_id, number, state, workspace_path, handle (provider ref), identity_sha256, image_digest, started_at, ended_at, exit_code, exit_class, timeout_at, drain_deadline, killed_at, termination_reason |
| `workers` | attempt_id PK, state, last_signal_at, last_signal |
| `leases` | id, kind, key, holder, fenced_token BIGINT, expires_at; UNIQUE (kind, key) |
| `heartbeats` | id BIGSERIAL, attempt_id, ts, signal, detail |
| `events` | seq BIGSERIAL PK, ts, kind, task_id, execution_id, attempt_id, principal, verified, payload JSONB |
| `log_chunks` | id BIGSERIAL, attempt_id, stream, offset_start, offset_end, ts, content BYTEA |
| `artifacts` | id, attempt_id, type, filename (logical), path (content-addressed, under artifact root), size, sha256, content_type, principal_id, created_at |
| `evidence` | id, attempt_id, kind, observed_at, source, verified, payload JSONB, artifact_id |
| `completion_claims` | attempt_id PK, document JSONB, parsed_ok, parse_errors JSONB |
| `gate_results` | id, attempt_id, phase (pre_pr, publication, post_pr), gate, result, evaluated_at, evidence_ids BIGINT[], detail |
| `acceptance_results` | id, task_id, head_sha, principal_id, verdict, reasoning, superseded_at, created_at |
| `decisions` | id, task_id, escalation_id, principal_id, verbatim TEXT, resolves, created_at |
| `escalations` | id, task_id, attempt_id, state, question, opened_at, closed_at |
| `wakes` | id, principal_id, task_id, reason, payload JSONB, created_at, delivered_at, acked_at, attempts |
| `supervisor_status` | singleton: holder, last_tick_at, last_success_at, last_error, consecutive_failures, tick_ms, counts JSONB |
| `idempotency_keys` | (principal_id, key) PK, request_sha256, response JSONB, created_at |
| `bootstrap_imports` | id, source_sha256, manifest JSONB, verified_at, committed_at, state |
| `repositories` | id, name UNIQUE, url, default_branch, installation_id, policy_name, registered_by, created_at |
| `worker_images` | digest PK, reference, harness, harness_version, build_inputs_sha256, promotion_state, promoted_at, promoted_by |
| `harnesses` | name PK, enabled (the administrator's flag, 25), reason, session_compatibility (unverified, verified, failed), mount_mode_observed, refresh_requires_rw, last_launch_at, last_launch_outcome, last_auth_failure_at, last_validated_at, updated_at, updated_by |
| `image_promotions` | digest PK, reference, harness, harness_version, state (candidate, default, retained), reason, updated_at, updated_by |
| `review_reports` | id, task_id, head_sha, reviewer_kind, reviewer_attempt_id, reviewer_principal_id, document JSONB, artifact_id, created_at |
| `pull_requests` | id, task_id UNIQUE, repository_id, number, url, base_ref, work_branch, state, head_sha, title, opened_at, merged_at, merge_sha, merged_by, closed_at, closed_by, last_polled_at, last_reactions_polled_at, reactions_observable, cancelled_at, body_sha256; UNIQUE (repository_id, number) |
| `pull_request_heads` | id, pull_request_id, sha, pushed_by (crucible, other), observed_at |
| `external_review_cycles` | id, pull_request_id, head_sha, components JSONB, completed_components JSONB, state (open, completed, superseded), trigger, opened_at, completed_at |
| `external_reviews` | id, pull_request_id, cycle_id, reviewer_login, signal (review, comment, reaction), github_id, reviewed_sha, sha_inferred (a reaction carries no commit id; the binding is inferred), state, body (stored only after secret scanning and redaction), body_sha256, accepted, received_at |
| `review_comments` | id, pull_request_id, external_review_id, github_id, kind, login, path, line, body (stored only after secret scanning and redaction), body_sha256, reviewed_sha, created_at, updated_at |
| `review_dispositions` | id, review_comment_id UNIQUE, principal_id, disposition, reasoning, created_at |
| `ci_certifications` | id, pull_request_id, head_sha, state, required_checks JSONB, check_runs JSONB, failure JSONB (check, workflow, job, log artifact), evaluated_at |
| `ci_decisions` | id, task_id, ci_certification_id, principal_id, cause, action, reasoning, created_at |
| `github_deliveries` | delivery_id PK, event, action, received_at, body_sha256, normalized JSONB (scanned and redacted fields only; never the raw body), processed_at |
| `reactions` | id, pull_request_id, subject_kind (pull_request, review, review_comment, issue_comment), subject_github_id, github_id, login, content, created_at, observed_at, removed_at (the observation time at which the reaction was gone, never a claim that it was deleted) |
| `release_contracts` | id, external_id, repository_id, target_branch, target_sha, version, tag, document JSONB, sha256, authorization_decision_id, submitted_at |
| `releases` | id, release_contract_id UNIQUE, state, tag_sha, tagged_at, workflow_run_url, conclusion, ended_at |
| `routing_policies` | (name, version) PK, document JSONB, created_at, retired_at |
| `attempt_metrics` | attempt_id PK, model, model_reported (the model the transcript named, null when the harness named none), harness, endpoint_kind, pool, wall_ms, tokens_in, tokens_out, cost_units, cost_source (harness_reported, none), exit_class, gates_passed, gates_failed, corrections_after, acceptance_verdict |
| `retention_actions` | id, policy_name, policy_version, kind, target, performed_at, event_seq |

Indexes: tasks (state), (principal_id, updated_at); events (task_id, seq);
attempts (state); leases (expires_at); wakes (principal_id, acked_at) partial
where acked_at is null.

Triggers: `events`, `task_contracts`, `release_contracts`, `review_dispositions`, and `ci_decisions` reject UPDATE and DELETE. No table ever holds a token, key, or secret; a CI check asserts no column name matches the secret-name pattern. Writes to
`executions`, `attempts`, `workers`, `heartbeats`, `gate_results`,
`completion_claims`, `attempt_metrics`, `evidence`, `log_chunks`, `retention_actions`, `supervisor_status`,
`pull_requests`, `pull_request_heads`, `external_review_cycles`,
`external_reviews`, `review_comments`, `reactions`, `ci_certifications`,
and `events` rows whose
`principal` is `crucible` require a transaction-local `crucible.fenced_token` (set with `SET LOCAL`
at the start of every supervisor transaction, never per connection, because
pooled connections would carry a stale value) exactly equal to the token on
the current `supervisor` lease row, read `FOR SHARE` so a takeover cannot
interleave; a BEFORE trigger rejects anything else (the principal check is
nested inside the trigger body because PL/pgSQL compiles `NEW.principal`
for every attached table).
`harnesses` and `image_promotions` are the exception to the fencing rule
above: the supervisor writes the observation columns (last launch, last
auth failure) and the admin surface writes the flags and promotion states,
so two principals write each table and neither is fenced to the supervisor
lease. Every admin mutation is still an event with its principal and
reason (25).

The API role writes only `tasks` (submit, start, cancel, amend, close),
`task_contracts`, `idempotency_keys` (in the same transaction as the
mutation they record), `acceptance_results`, `decisions`, `artifacts`,
`wakes` (ack), `policies`, `routing_policies`, `review_reports`, and
`github_deliveries` (the webhook endpoint holds no supervisor lease and has
no authenticated principal, so a delivery is neither fenced nor append-only;
the supervisor processes it afterwards, and its events are recorded under
the principal `github` rather than `crucible` for the same reason); `evidence`
is the supervisor's alone, so an uploaded artifact or review report becomes
evidence on the next tick, never inside the request; it enqueues everything that touches an attempt for the
supervisor.

## Strategy

- SQLAlchemy 2.x typed ORM with explicit `Mapped[]` annotations; no
  implicit lazy loading in the application layer (repositories return
  domain objects).
- Alembic migrations, one file per change, hand-reviewed (autogenerate as
  a draft only). `crucible-admin migrate` applies; the container entrypoint
  refuses to serve if the head revision is not applied, and `/ready` reports
  it.
- Down migrations required for every revision in v0.x.
- A down migration that narrows the event-kind constraint has to deal with
  the rows the newer kinds wrote. Revision 0007 (C4) **archives** them: it
  copies every C4 event row into `events_c4_archive`, removes them from
  `events`, recreates the older constraint in its ordinary validating form,
  and its upgrade moves the rows back. Nothing is lost either way, and a
  database that was downgraded says so by having the archive table. This is
  the pattern for future revisions. It is not retroactive: earlier
  downgrades (0006, for one) simply delete the event rows of the kinds they
  remove, which is a known gap, not a promise kept.
- An applied migration is never edited. Before the first tagged release
  the initial revision may be squashed only together with a documented
  `make reset`; after it, every change is a new revision. Readiness
  compares the live schema to the ORM metadata so a rewritten or
  half-applied migration is reported, not masked by the revision id (found
  in C1 verification, 2026-09-16).
- Migrations tested in CI against a fresh database and against a database
  seeded at the previous release's head.
- JSONB documents validated by Pydantic on the way in and out; schema
  versions inside documents, table versions by Alembic. A document schema
  bump that changes stored shape ships with a data migration.
- Backups are the operator's concern (volume snapshot locally; operator
  tooling on the cluster). `crucible-admin export` produces a portable JSON
  bundle for the same reason the bootstrap import exists.
