# 01. Architecture and trust boundaries

## Shape: modular monolith, API first

One deployable Python service with a strict internal module layout. The
domain core knows nothing about FastAPI, SQLAlchemy, Docker, or Kubernetes.
Adapters at the edge translate.

```
crucible/
  domain/         entities, value objects, state machines, gate evaluation
                  (pure Python, no I/O, no framework imports)
  contracts/      versioned Pydantic models: task contract, API schemas,
                  worker identity, worker report, events
  application/    use cases: submit task, schedule, launch, ingest report,
                  evaluate gates, publish, observe PR, certify CI, release,
                  reconcile, cancel, retain, import bootstrap ledger
  ports/          abstract interfaces the application depends on:
                  Repository, ExecutionProvider, HarnessAdapter,
                  ArtifactStore, Notifier, Clock, GitHost (push, PR,
                  reviews, checks, tags), TokenMinter
  adapters/
    api/          FastAPI routers for /v1, auth, OpenAPI, GitHub webhook receiver
    persistence/  SQLAlchemy models, repositories, Alembic migrations
    execution/    fake, docker, (kubernetes later), hostprocess
    harness/      claude_code, codex, agy launch specs and report parsers
    artifacts/    filesystem store (object store later)
    notify/       webhook, poll queue
    github/       GitHub App JWT and installation tokens, REST client,
                  delivery parsing, publisher job
  scheduler/      the supervision loop: leases, heartbeats, timeouts,
                  reconciliation ticks
  cli/            crucible: serve, admin (migrate, import, tokens, ...), orchestrator verbs
```

Dependency direction is inward only: adapters import application and
domain; domain imports nothing above it. Enforced by an import-linter rule
in CI.

## Transport

PostgreSQL carries authoritative state, durable events, wakes, leases, and
queued work: polling, transactional claiming (`SELECT ... FOR UPDATE SKIP
LOCKED`), and database-backed leases. No Redis, NATS, or message broker
(ADR 0013). If measurement later shows PostgreSQL cannot meet
event-delivery or queue-throughput needs, a transport is evaluated by ADR;
infrastructure is not added on hypothetical scale.

## Processes

Two long-running roles, one image:

- **api**: serves `/v1`. Stateless; every request is a transaction.
- **supervisor**: the scheduler loop. Exactly one active instance, enforced
  by a database lease (`supervisor_lease` row, renewed every tick). A second
  instance waits as standby. Ticks: claim due work, launch, poll providers,
  renew leases, expire stale leases, evaluate gates, emit wakes, clean up.

Locally both run in one container by default (`crucible serve --all`).
They split when deployed to Kubernetes.

## Trust boundaries

```
+-----------------------------------------------------------+
| Workstation (trusted operator)                             |
|                                                            |
|  Foundry (Claude Code / Codex / AGY session)               |
|     |  HTTPS + bearer token                                |
|     v                                                      |
|  +---------------------- Docker Compose ----------------+  |
|  |  crucible (api + supervisor)      postgres           |  |
|  |     | docker socket proxy (restricted; rootless      |
  |     |   daemon preferred, 13)                        |  |
|  |     v                                                |  |
|  |  worker container (per attempt)                      |  |
|  |    - repo checkout volume (rw, per attempt)          |  |
|  |    - identity + contract mount (ro)                  |  |
|  |    - one harness credential mount (ro or narrow rw)  |  |
|  |    - NO docker socket, NO crucible token, NO db,     |  |
|  |      NO GitHub credential                            |  |
|  |  publisher container (per push or tag job)           |  |
|  |    - bundle (ro), tmpfs token file, github.com only  |  |
|  +------------------------------------------------------+  |
|          ^ GitHub webhooks (HMAC) and polling                |
+-----------------------------------------------------------+
```

Three trust levels:

1. **Operator and Foundry**: fully trusted. Hold the API token.
2. **Crucible**: trusted control plane. Holds the database credential, the
   docker socket proxy endpoint, the GitHub App private key, and read
   access to the credential directories it mounts into workers. It is the
   primary security boundary around workers and the only party that
   writes to GitHub.
3. **Workers**: untrusted. They run model-driven code with whatever the
   harness permits (usually everything, since harness sandboxes are bypassed
   or unavailable). Containment is the container: no socket, no Crucible
   credentials, no other harness's credentials, no GitHub credential,
   network as policy allows, filesystem limited to the checkout and mounts,
   resource limits applied. A worker can misuse its own harness credential
   within the egress allowlist; 12 states the mitigations.

The worker reports back by writing files into a designated report directory
inside the checkout mount (which Crucible reads after exit), never by calling
the API. Workers hold no API token.

## Local topology (Docker Compose)

Normal mode: `crucible`, `postgres`, and `docker-socket-proxy` are Compose
services. Workers are sibling containers created through the proxy, labeled
`crucible.attempt=<id>`, on a dedicated Docker network. Closing a Foundry
session touches none of this.

Developer mode: `postgres` and the proxy run under Compose; `crucible` runs
from the developer's virtualenv with reload. Same API, same contracts, same
provider. Workers still run as containers.

Detail in 13-local-operation.md.

## Kubernetes topology (built in C8; detail in 26)

`api` Deployment, `supervisor` Deployment (replicas 1, lease-guarded),
PostgreSQL (operator-managed or external), workers as Jobs created through
the Kubernetes API with a ServiceAccount scoped to one namespace. No Docker
socket anywhere. Detail in 26-kubernetes-provider.md.

## Cross-cutting

- **Time**: all timestamps stored as UTC with offset; rendered for people
  in the operator's configured zone (`service.render_timezone`, default
  UTC in the repository, `America/Chicago` in the operator's private
  configuration). Never epoch in any API or log.
- **IDs**: ULIDs for every entity; stable task IDs from an orchestrator may be
  supplied as `external_id` and are unique per orchestrator namespace.
- **Logging**: structured JSON to stdout, one event per line, with
  `task_id`, `execution_id`, `attempt_id` where present.
- **Config**: Pydantic settings from environment and an optional TOML file.
  Sanitized example in `examples/config/`.
