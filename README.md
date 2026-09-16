# Crucible

Crucible is a deterministic supervisor for AI coding workers. It accepts an
explicit, versioned task contract, launches a worker harness (Claude Code,
Codex, or AGY) in an isolated execution environment, records everything the
worker does as durable events, logs, artifacts, and evidence, enforces
mechanical completion gates, and reports state through a versioned HTTP API.

It does not decide what to build. An orchestrator (Foundry, or a person)
decides outcomes, scope, model, and acceptance. Crucible executes, persists,
observes, and enforces.

**Status: specification.** No implementation exists yet. The specification is
under [`docs/spec/`](docs/spec/00-overview.md) and the decisions behind it
under [`docs/adr/`](docs/adr/). Implementation starts only after the
specification is approved.

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

## What it will not do

Interpret ambiguous requirements, choose architecture, invent acceptance
criteria, broaden scope, pick a model by judgment, create follow-up work on
its own, approve semantic correctness, accept a risk, or declare success.

## Layout

```
docs/spec/      the specification, one concern per file
docs/adr/       architectural decision records
examples/       sanitized example task contracts and configuration
```

## License

MIT. See [LICENSE](LICENSE).
