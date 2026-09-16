# 04. Versioned API contracts

Base path `/v1`. JSON only. OpenAPI generated from the Pydantic models in
`crucible/contracts/` and published at `/v1/openapi.json`. Breaking changes
create `/v2`; `/v1` keeps serving for at least one minor release after.

## Authentication

Bearer tokens, created by `crucible-admin token create --principal <name>
--role <role>`. Stored as salted hashes. Roles:

| Role | May |
|---|---|
| `orchestrator` | everything below except admin |
| `operator` | everything an orchestrator may, plus record decisions of kind `release_authorization` and other operator-only decision kinds |
| `observer` | GET only |
| `admin` | token management, repository registration, bootstrap import, policy upload, forced reconcile, image promotion |

Every mutating request records `principal` on the resulting event. Workers
never hold a token. The GitHub webhook endpoint uses HMAC verification
instead of a bearer token (below).

## Conventions

- IDs are ULIDs. Orchestrator-stable IDs travel in `external_id`.
- Idempotency: every POST that creates accepts `Idempotency-Key`; a repeat
  with the same key and body returns the original result; the same key with
  a different body is 422 with problem type `idempotency-key-reuse`.
- Pagination: `?limit=&cursor=` on every list; responses carry `next_cursor`.
- Errors: RFC 9457 problem details with a stable `type` URI per error class.
- Time: RFC 3339 with offset in responses; requests accept the same.
- Every response includes `schema_version` of the resource.

## Endpoints

### Tasks

| Method | Path | Purpose |
|---|---|---|
| POST | `/tasks` | Submit a task contract (body: `TaskContractV1`). Validates, persists, returns the task in `submitted`. Does not launch. |
| GET | `/tasks` | List with filters: `state`, `project`, `repository`, `external_id`, `updated_since`. |
| GET | `/tasks/{id}` | Full task view: contract versions, executions, latest attempt summary, gate summary, PR summary, open escalations. |
| POST | `/tasks/{id}/start` | Move to `scheduled`; body names harness, model, image, provider, policy version, and optional overrides. This is Foundry's dispatch decision. Overrides create an amendment (05); until the amendment path exists (C2) the body must agree with the contract. |
| POST | `/tasks/{id}/cancel` | Request cancellation; body carries reason and the deciding principal's verbatim words. The API writes the task state and enqueues termination for the supervisor. |
| POST | `/tasks/{id}/amend` | Attach a new contract version; allowed only in `submitted`, `blocked`, or `awaiting_acceptance`. |
| POST | `/tasks/{id}/review` | Request the internal non-author review of the current collected head. Body either names an execution request for a Crucible `review` execution, or carries an uploaded `ReviewReportV1` produced by the orchestrator through its own harness. Allowed in `awaiting_internal_review`. |
| POST | `/tasks/{id}/accept` | Record an `AcceptanceResult` (accepted, rejected, needs_more_work) with reasoning for the current collected head. Orchestrator role only. |
| POST | `/tasks/{id}/corrections` | Attach a correction: a new contract version whose `correction` section names the review comments or CI findings it addresses, plus an execution request. Creates a `correct` execution against the existing remote branch. Allowed in `pre_pr_gates_failed`, `external_feedback_received`, `ci_certification_failed`, and after `needs_more_work`. |
| POST | `/tasks/{id}/dispositions` | Record `ReviewDisposition` rows for received external review comments. Orchestrator role. |
| POST | `/tasks/{id}/head-decision` | In `head_diverged`: `recollect` (Crucible collects the new remote head and the task re-enters `reported`), `reject`, or `cancel`, with reasoning. |
| POST | `/tasks/{id}/ci-decision` | In `ci_certification_failed`: record the cause Foundry determined (enum in 23) and the action: `rerun` (recorded; the operator re-runs on GitHub, 23), `correct` (followed by a correction), `reject`, or `cancel`. |
| POST | `/tasks/{id}/decisions` | Record a `Decision` (verbatim text, who, what it resolves). |
| POST | `/tasks/{id}/close` | Orchestrator closes an `accepted`, `merged`, or `released` task. |
| GET | `/tasks/{id}/events` | Ordered events for the task and its children. |
| GET | `/tasks/{id}/pull-request` | The PR record with head history, external reviews, dispositions, and CI certifications. |
| GET | `/events` | Global feed, `?cursor=&kind=&since=`. |

### Executions and attempts

| Method | Path | Purpose |
|---|---|---|
| GET | `/executions/{id}` | Execution with its attempts. |
| POST | `/executions/{id}/retry` | Create a new attempt now, if policy permits; body carries reason. |
| GET | `/attempts/{id}` | Attempt with worker, lease, heartbeat summary, image digest, exit info. |
| GET | `/attempts/{id}/logs` | Log chunks; `?stream=stdout|stderr&offset=`; `Accept: text/event-stream` for live tail. |
| GET | `/attempts/{id}/artifacts` | List artifacts with type, size, sha256. |
| POST | `/attempts/{id}/artifacts` | Upload an artifact (multipart: type, file). Principal recorded. Orchestrator role. |
| GET | `/artifacts/{id}` | Metadata; `/artifacts/{id}/content` streams bytes. |
| GET | `/attempts/{id}/report` | Parsed `CompletionClaimV1` or 404 if none. |
| GET | `/attempts/{id}/gates` | Gate results with evidence links. |
| POST | `/attempts/{id}/terminate` | Stop the worker: `mode=drain|kill`, reason, verbatim words. |

### Releases

| Method | Path | Purpose |
|---|---|---|
| POST | `/releases` | Submit a `ReleaseContractV1` (24). Validates, verifies the referenced authorization decision exists, returns the release in `submitted`. Orchestrator role. |
| GET | `/releases/{id}` | Release with gate results, tag, observed workflow run, outcome. |
| POST | `/releases/{id}/cancel` | Withdraw before tagging. |

### Supervision and health

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (process up). No auth. |
| GET | `/ready` | Readiness, no auth (compose healthchecks and Foundry's probe need it). True only when all hold: database reachable; migrations at head **and** the live schema matches the ORM metadata (schema drift is not-ready, naming the first difference); the supervisor lease is held and the holder's last tick within the lease window succeeded. A supervisor whose ticks are failing makes the service not-ready with the last error summary, even though the lease is renewed. |
| GET | `/supervisor` | Lease holder, last tick, tick duration, queue depths, provider status, GitHub observation status (last poll, webhook deliveries pending). |
| POST | `/supervisor/reconcile` | Force a reconciliation pass now. Admin. |
| GET | `/wakes` | Pending wakes for the caller's principal; `?since=`. |
| POST | `/wakes/{id}/ack` | Mark handled, with what was done. |

### Policies, repositories, harnesses, images, providers

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/policies/{name}/{version}` | Read or upload a policy document. Versions are immutable once referenced. |
| GET/PUT | `/repositories/{name}` | Register a target repository: URL, installation reference, default branch, policy. Admin. Never carries a credential. |
| GET | `/harnesses` | Supported harnesses: adapter supported version range, installed versions per image, credential requirements, capability flags. |
| GET | `/images` | Worker images known to Crucible: harness, version, digest, promotion state. |
| POST | `/images/{digest}/promote` | Set promotion state (`default`, `retained`, `retired`). Admin; records the decision. |
| GET | `/providers` | Registered execution providers and their capabilities. |
| GET/PUT | `/routing/{name}/{version}` | Read or upload a routing policy (05b). Admin. |
| GET | `/routing/usage` | Per-pool usage in the current window, from AttemptMetrics. |
| GET | `/routing/history` | Per-model outcomes: `?model=&project=&since=`; wall time, cost where reported, exit class, gates passed, corrections, acceptance. Foundry reads this before selecting. |

### Administration

`/v1/admin/*`, admin role, versioned with the rest: status, harnesses, credentials (validate, probe, login, rotate, remove), images, providers, github, repositories, audit. Detail in 25. `GET /v1/capabilities` gives orchestrator principals the sanitized read-only subset Foundry needs to report an unavailable capability.

### GitHub ingress

| Method | Path | Purpose |
|---|---|---|
| POST | `/github/webhook` | Optional accelerator, off by default locally (23). Verifies the `X-Hub-Signature-256` HMAC against the raw request body in memory; unsigned or mismatched deliveries are rejected and counted, and nothing of them is stored. An accepted delivery is parsed and normalized in memory to the fields Crucible uses (delivery ID, event and action, repository, PR number, head SHA, review or comment IDs, login, reviewed SHA, check conclusion), user-controlled text is passed through the secret scanner and redaction before it is kept, and only that normalized record plus a SHA-256 of the original body is stored. The raw body is never persisted. Deduplicated by delivery ID. Processed by the supervisor tick. |

### Bootstrap import

| Method | Path | Purpose |
|---|---|---|
| POST | `/import/bootstrap` | Accept a `BootstrapExportV1` bundle; returns a verification report. Admin. Detail in 15. |
| POST | `/import/bootstrap/{id}/commit` | Make the imported records authoritative after verification. |

## Wake delivery

Wakes are rows first. Delivery is best-effort POST to the configured webhook
with retry and backoff; `/wakes` is the durable fallback that Foundry polls on
every start-of-session. Detail in 17.

## Versioning of contracts inside the API

`TaskContractV1`, `CompletionClaimV1`, `ReviewReportV1`, `WorkerIdentityV1`,
`PolicyV1`, `ReleaseContractV1`, `EventV1`, `WakeV1`, `BootstrapExportV1`.
Each carries `schema_version`. The API rejects unknown major versions and
records the rejection as an event on the principal.
