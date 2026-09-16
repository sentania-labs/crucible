# Crucible

Crucible is a deterministic supervisor for AI coding workers. It accepts an
explicit, versioned task contract, launches a worker harness (Claude Code,
Codex, or AGY) in an isolated execution environment, records everything the
worker does as durable events, logs, artifacts, and evidence, enforces
mechanical completion gates, and reports state through a versioned HTTP API.

It does not decide what to build. An orchestrator (Foundry, or a person)
decides outcomes, scope, model, and acceptance. Crucible executes, persists,
observes, and enforces.

**Status: specification version 0.3; implementation phase C1 (walking
skeleton).** The specification is under
[`docs/spec/`](docs/spec/00-overview.md) and the decisions behind it under
[`docs/adr/`](docs/adr/). Phase notes are under
[`docs/implementation-notes/`](docs/implementation-notes/c1.md).

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
```

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

## Layout

```
crucible/       the service: domain, contracts, application, ports, adapters, scheduler, cli
tests/          unit (no I/O) and integration (PostgreSQL in a container, fake provider)
docs/spec/      the specification, one concern per file
docs/adr/       architectural decision records
docs/implementation-notes/  what each phase decided where the spec was open
examples/       sanitized example task and release contracts and configuration
```

## License

MIT. See [LICENSE](LICENSE).
