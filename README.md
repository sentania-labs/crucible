# Crucible

Crucible is a deterministic supervisor for AI coding workers. It accepts an
explicit, versioned task contract, launches a worker harness (Claude Code,
Codex, or AGY) in an isolated execution environment, records everything the
worker does as durable events, logs, artifacts, and evidence, enforces
mechanical completion gates, and reports state through a versioned HTTP API.

It does not decide what to build. An orchestrator (Foundry, or a person)
decides outcomes, scope, model, and acceptance. Crucible executes, persists,
observes, and enforces.

**Status: specification version 0.3; implementation phase C2 (gates, claims,
review, acceptance, wakes).** The specification is under
[`docs/spec/`](docs/spec/00-overview.md) and the decisions behind it under
[`docs/adr/`](docs/adr/). Phase notes are under
[`docs/implementation-notes/`](docs/implementation-notes/c2.md).

## What it will do

- Validate a task contract before anything runs.
- Persist tasks, workers, executions, attempts, events, artifacts, evidence,
  decisions, and gate results in PostgreSQL, with explicit migrations.
- Launch workers as ephemeral containers (Docker locally, Kubernetes later),
  each with its own repository checkout, injected identity, and only the
  credentials its harness needs.
- Detect completion, failure, timeout, cancellation, stall, and loss with
  leases and heartbeats, and reconcile after its own restart.
- Evaluate deterministic definition-of-done gates and record the evidence.
- Keep authorized work running while the orchestrator is disconnected, and
  wake it when judgment is required.
- Own the routine GitHub mutations through a narrowly scoped GitHub App:
  push the verified branch, open the PR with a body built from verified
  evidence, watch external review and CI, observe the merge, and tag a
  release only from an explicit, operator-authorized release contract.
- Never give a worker a GitHub credential, the Docker socket, or another
  harness's credentials.

## What it will not do

Interpret ambiguous requirements, choose architecture, invent acceptance
criteria, broaden scope, pick a model by judgment, create follow-up work on
its own, approve semantic correctness, accept a risk, declare success,
interpret review feedback, merge a PR, or decide that a release should
happen.

## Running locally

Needs Docker with Compose, and [uv](https://docs.astral.sh/uv/) for the
developer mode and the test suites.

```sh
make up          # postgres, migrations, crucible (api + supervisor) on 127.0.0.1:8080
make dev         # postgres only; then: uv run crucible serve --all
make lint        # ruff, mypy --strict, import-linter
make test        # unit tier, then the integration tier against postgres:16 in a container
make down
make reset       # DESTRUCTIVE: down and delete the postgres and artifact volumes
```

`/v1/ready` reports not ready with "schema drift" when the live schema does not
match what the code expects (for example a database created by an earlier
build of this branch). Before the first tagged release the answer is
`make reset`, which deletes the database; after it, a migration.

`make up` copies `.env.example` to `.env` if none exists; change
`POSTGRES_PASSWORD` there. First use after `make up`:

```sh
docker compose exec crucible crucible-admin token create --principal foundry --role orchestrator
docker compose exec crucible crucible-admin repository register --name example-service \
  --url https://github.com/example-org/example-service
curl -s http://127.0.0.1:8080/v1/ready
```

Then `POST /v1/tasks` with a `TaskContractV1` whose `execution_request.provider`
is `fake` and whose image is `crucible-worker:fake-succeed`, `POST
/v1/tasks/{id}/start`, and watch `GET /v1/tasks/{id}/events`. OpenAPI is at
`/v1/openapi.json`.

## The supervision half, end to end

A run on the fake provider goes contract, attempt, collected head, gates,
internal review, acceptance:

```sh
GET  /v1/tasks/{id}                     # state, head_sha, gate_summary, review, acceptance
GET  /v1/attempts/{id}/gates            # one row per pre-PR gate, with its evidence ids
GET  /v1/attempts/{id}/evidence         # what the gates read; a worker row is never verified
GET  /v1/attempts/{id}/report           # the parsed CompletionClaimV1
POST /v1/tasks/{id}/review              # upload a ReviewReportV1, or ask for a review execution
POST /v1/tasks/{id}/accept              # Foundry's AcceptanceResult; Crucible never infers one
POST /v1/tasks/{id}/corrections         # a narrowed contract version and a `correct` execution
GET  /v1/wakes                          # what needs judgment; poll is the durable path
POST /v1/wakes/{id}/ack                 # what you did about it
```

The gate rows say `pending` for `verification_ran` and `workspace_clean`, whose
verifier container arrives in C3, and for `internal_review_recorded` until a
non-author review of that exact head exists. An `artifacts` deliverable reaches
`accepted`; a `branch` or `pull_request` one records the acceptance and waits for
the publisher, which arrives in C4.

Policies and the routing policy they name are documents, not defaults in code:

```sh
GET  /v1/policies/default-software/1    # every tunable the specification mentions
PUT  /v1/policies/{name}/{version}      # admin; a version a task references is immutable
GET  /v1/routing/default-routing/1      # the models Foundry may name, per tier
GET  /v1/routing/usage                  # per-pool usage in the current window
GET  /v1/routing/history?model=&project=  # what each model actually did
```

`examples/policies/default-software.yaml` is the seeded default, verbatim.

## Releases

A release is a tag push, not a merge: `git tag -a vX.Y.Z -m vX.Y.Z && git push
origin vX.Y.Z` on `main`. The version is derived from that tag (`hatch-vcs`), so
nothing in the tree pins it and `/v1/health` reports what was tagged. The
release workflow refuses a tag that is not `vMAJOR.MINOR.PATCH` or whose commit
is not reachable from `main`, builds and smokes the image, publishes
`ghcr.io/sentania-labs/crucible:<version>` to GHCR, and then cuts the GitHub
release. Details: [docs/implementation-notes/release.md](docs/implementation-notes/release.md).

## Layout

```
crucible/       the service: domain, contracts, application, ports, adapters, scheduler, cli
tests/          unit (no I/O) and integration (PostgreSQL in a container, fake provider)
docs/spec/      the specification, one concern per file
docs/adr/       architectural decision records
docs/implementation-notes/  what each phase decided where the spec was open
examples/       sanitized example task and release contracts, policies, and configuration
```

## License

MIT. See [LICENSE](LICENSE).
