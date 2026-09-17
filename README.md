# Crucible

Crucible is a deterministic supervisor for AI coding workers. It accepts an
explicit, versioned task contract, launches a worker harness (Claude Code,
Codex, or AGY) in an isolated execution environment, records everything the
worker does as durable events, logs, artifacts, and evidence, enforces
mechanical completion gates, and reports state through a versioned HTTP API.

It does not decide what to build. An orchestrator (Foundry, or a person)
decides outcomes, scope, model, and acceptance. Crucible executes, persists,
observes, and enforces.

**Status: specification version 0.3; implementation phase C4 (GitHub delivery).**
The specification is under [`docs/spec/`](docs/spec/00-overview.md) and the
decisions behind it under [`docs/adr/`](docs/adr/). Phase notes are under
[`docs/implementation-notes/`](docs/implementation-notes/c4.md).

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
make up          # postgres, the two proxies, migrations, crucible on 127.0.0.1:8080
make dev         # postgres and the proxies; then: uv run crucible serve --all
make proxies     # the socket proxy and the egress proxy only
make lint        # ruff, mypy --strict, import-linter
make test        # unit tier, then the integration tier against postgres:16 in a container
make smoke       # after `make up`: drive one task end to end; the same script CI and release run
make e2e-image   # the script-harness worker image the e2e tier runs
make e2e         # the Docker provider against real containers, no model
make e2e-github  # local only: the real GitHub App against a throwaway repository
make down
make reset       # DESTRUCTIVE: down and delete the postgres and artifact volumes
```

### The Docker provider

Workers run on a **dedicated rootless Docker daemon** owned by a service user,
behind `docker-socket-proxy`; Crucible never sees the raw socket. Point the
Makefile at that daemon, which on a host where the service user has no login
shell means a wrapper:

```sh
make e2e DOCKER='sudo -u crucible -H env HOME=/var/lib/crucible-docker \
  XDG_RUNTIME_DIR=/run/user/$(id -u crucible) \
  DOCKER_HOST=unix:///run/user/$(id -u crucible)/docker.sock docker'
```

Nothing else changes: CI has an ordinary daemon and needs none of it. What a
worker gets is a checkout whose origin resolves nowhere, a read-only identity
bundle, a writable report directory, `--init`, uid 1000, all capabilities
dropped, `no-new-privileges`, a read-only root, and an `internal: true` network
whose only way out is the egress proxy's hostname allowlist. It never receives
the socket, the proxy endpoint, the database, or another harness's credential,
and `make e2e` asserts each of those from inside a real worker.

`make proxy-config` writes the egress proxy's allowlist from `EGRESS_ALLOWLIST`;
Crucible refuses to launch an attempt that needs a hostname the running proxy
does not permit, rather than letting it fail quietly on the network.

`/v1/ready` reports not ready with "schema drift" when the live schema does not
match what the code expects (for example a database created by an earlier
build of this branch). Before the first tagged release the answer is
`make reset`, which deletes the database; after it, a migration.

`make up` copies `.env.example` to `.env` if none exists; change
`POSTGRES_PASSWORD` there. First use after `make up`:

```sh
docker compose exec crucible crucible-admin token create --principal foundry --role orchestrator
docker compose exec crucible crucible-admin repository register --name example-service \
  --url https://github.com/example-org/example-service \
  --installation-id 0 --attest-external-review-all-prs
curl -s http://127.0.0.1:8080/v1/ready
```

Then `POST /v1/tasks` with a `TaskContractV1` whose `execution_request.provider`
is `fake` and whose image is `crucible-worker:fake-succeed` (or `docker` with a
worker image the policy allowlist admits), `POST
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

The gate rows say `pending` for `internal_review_recorded` until a non-author
review of that exact head exists. An `artifacts` deliverable reaches `accepted`;
a `branch` or `pull_request` one moves to `publishing`, where the supervisor
pushes the verified head and opens the pull request.

## The delivery half, end to end

Crucible, not the worker and not the orchestrator, performs every routine GitHub
mutation and watches the pull request afterwards:

```sh
GET  /v1/tasks/{id}/pull-request        # heads, review cycles, comments, reactions, CI
POST /v1/tasks/{id}/dispositions        # Foundry's reading of one review comment
POST /v1/tasks/{id}/ci-decision         # cause and action for a required CI failure
POST /v1/tasks/{id}/head-decision       # recollect, reject, or cancel a diverged head
POST /v1/github/webhook                 # optional accelerator, HMAC, off by default
```

A task that is accepted goes `publishing`, then `awaiting_external_review` or
`awaiting_ci_certification`, then `ready_for_merge`, and reaches `merged` when
Crucible observes the merge. **Merging is the operator's act: Crucible has no
merge endpoint.** A required CI failure lands in `ci_certification_failed` with
the check, the head, and a log excerpt, and never retries by itself. A head
Crucible did not push moves the task to `head_diverged` and supersedes that
head's acceptance, review, and gates.

Delivery needs a GitHub App with Metadata read, Contents read/write, Pull
requests read/write, Checks read, Actions read, and Issues read (the last for
reactions on the pull request, which is the only place a clean external review
appears). The App id is configuration; the private key and the webhook secret
are *paths* to files mounted read-only into the `crucible` container alone.
Installation tokens are minted per job, live in memory and in the publisher
container's tmpfs, and are never in configuration, the environment, a log, an
event, or the database. Workers hold no GitHub credential at all.

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
