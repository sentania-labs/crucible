# Security

Crucible is control-plane software. In its local Docker mode it holds
host-level authority through the Docker socket (see
`docs/spec/13-local-operation.md` for what that authority is and how it is
restricted). Treat any vulnerability that lets a worker container reach the
Docker socket, another harness's credentials, or the Crucible database as
critical.

## Reporting

Report vulnerabilities privately through GitHub's security advisory form for
this repository ("Report a vulnerability" under the Security tab). Do not open
a public issue for a security problem. Expect an acknowledgement within seven
days.

## Scope

In scope: the Crucible API and service, its execution providers, harness
adapters, credential handling, and the Docker Compose and Kubernetes
deployment shapes shipped in this repository.

Out of scope: vulnerabilities in the worker harnesses themselves (Claude Code,
Codex, AGY) or in the models behind them. Report those upstream.

## Never in this repository

Credentials, tokens, harness authentication state, worker transcripts, task
payloads from private projects, or infrastructure details of any specific
deployment. If you find one, report it the same way.
