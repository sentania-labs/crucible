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
| `observer` | GET only |
| `admin` | token management, bootstrap import, policy upload, forced reconcile |

Every mutating request records `principal` on the resulting event. Workers
never hold a token.

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
| GET | `/tasks` | List with filters: `state`, `project`, `external_id`, `updated_since`. |
| GET | `/tasks/{id}` | Full task view: contract, executions, latest attempt summary, gate summary, open escalations. |
| POST | `/tasks/{id}/start` | Move to `scheduled`; body names harness, model, provider, policy version, and optional overrides. This is Foundry's dispatch decision. |
| POST | `/tasks/{id}/cancel` | Request cancellation; body carries reason and the deciding principal's verbatim words. The API writes the task state and enqueues termination for the supervisor; it never writes attempt rows itself. |
| POST | `/tasks/{id}/amend` | Attach a new contract version; allowed only in `submitted`, `blocked`, or `awaiting_acceptance`. |
| POST | `/tasks/{id}/accept` | Record an `AcceptanceResult` (accepted, rejected, needs_more_work) with reasoning. Orchestrator role only. |
| POST | `/tasks/{id}/decisions` | Record a `Decision` (verbatim text, who, what it resolves). |
| POST | `/tasks/{id}/close` | Orchestrator closes an accepted task. |
| GET | `/tasks/{id}/events` | Ordered events for the task and its children. |
| GET | `/events` | Global feed, `?cursor=&kind=&since=`. |

### Executions and attempts

| Method | Path | Purpose |
|---|---|---|
| GET | `/executions/{id}` | Execution with its attempts. |
| POST | `/executions/{id}/retry` | Create a new attempt now, if policy permits; body carries reason. |
| GET | `/attempts/{id}` | Attempt with worker, lease, heartbeat summary, exit info. |
| GET | `/attempts/{id}/logs` | Log chunks; `?stream=stdout|stderr&offset=`; `Accept: text/event-stream` for live tail. |
| GET | `/attempts/{id}/artifacts` | List artifacts with type, size, sha256. |
| POST | `/attempts/{id}/artifacts` | Upload an artifact (multipart: type, file). Principal recorded; used for review artifacts and operator evidence. Orchestrator role. |
| GET | `/artifacts/{id}` | Metadata; `/artifacts/{id}/content` streams bytes. |
| GET | `/attempts/{id}/report` | Parsed `CompletionClaimV1` or 404 if none. |
| GET | `/attempts/{id}/gates` | Gate results with evidence links. |
| POST | `/attempts/{id}/terminate` | Stop the worker: `mode=drain|kill`, reason, verbatim words. |

### Supervision and health

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (process up). No auth. |
| GET | `/ready` | Readiness: database reachable, migrations current, supervisor lease held by some instance within the window. |
| GET | `/supervisor` | Lease holder, last tick, tick duration, queue depths, provider status. |
| POST | `/supervisor/reconcile` | Force a reconciliation pass now. Admin. |
| GET | `/wakes` | Pending wakes for the caller's principal; `?since=`. |
| POST | `/wakes/{id}/ack` | Mark handled, with what was done. |

### Policies, harnesses, providers

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/policies/{name}/{version}` | Read or upload a policy document. Versions are immutable once referenced. |
| GET | `/harnesses` | Supported harnesses, pinned versions, credential requirements, capability flags. |
| GET | `/providers` | Registered execution providers and their capabilities. |

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

`TaskContractV1`, `CompletionClaimV1`, `WorkerIdentityV1`, `PolicyV1`,
`EventV1`, `BootstrapExportV1`. Each carries `schema_version`. The API
rejects unknown major versions and records the rejection as an event on the
principal.
