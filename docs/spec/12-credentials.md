# 12. Credential and secret handling

## Principles

1. Crucible never stores a credential. Not in PostgreSQL, contracts, API
   payloads, images, git, logs, artifacts, reports, or retained artifacts.
   Secret scanning runs on every artifact and report before storage, on
   every contract at submit, and on every rendered PR body and tag message
   before it leaves.
2. One worker, one harness, one credential set. A Codex worker cannot see
   Claude Code's OAuth state, and vice versa.
3. Credentials reach a worker only as a mount from a source Crucible can
   read, referenced by name (`credential:codex`) in configuration, never
   by value in any API body.
4. Read-only by default. A narrow writable volume only when a harness must
   refresh its own auth state, and then only the named auth files.
5. Workers hold no GitHub credential, no Crucible API token, no database
   URL, no Docker endpoint. Verified by an integration test that greps the
   worker's environment and filesystem and attempts a push.
6. Installation tokens for GitHub are minted per job, live in memory and in
   the publisher container's tmpfs, and are discarded with the job.

## What a worker can do with its own credential, stated honestly

Container isolation keeps a worker away from other harnesses' credentials,
from GitHub, and from host resources. It does not protect the worker's own
harness credential from that worker: the process that must read the file
to authenticate can also read it to exfiltrate or misuse it within the
egress allowlist. Initial mitigations:

- one harness credential set per worker, disposable and narrow;
- no GitHub credential inside workers;
- strict egress allowlist, so the credential can only be used against the
  harness's own endpoints;
- narrow disposable credential copies, removed immediately after validated
  sync (16);
- secret redaction on logs and scanning on artifacts;
- no cross-harness credential access;
- per-harness concurrency of one.

A credential broker or authentication proxy that keeps the token outside
the container is a possible later hardening (ADR 0007), not an initial
requirement.

## Onboarding

Credentials enter Crucible only through the administrative onboarding
workflow (25): a dedicated directory, the harness's own interactive login
pointed at it, validation, a bounded probe in the hardened image, and the
daily-session compatibility test (21, S1b) before the harness is enabled.
The operator's daily-use directories are never copied.

## Credential sources (config, sanitized example in `examples/config/`)

```toml
[credentials.claude_code]
source = "directory"
path = "/var/lib/crucible/credentials/claude_code"   # contains the CLI's auth state
mount_mode = "rw-narrow"       # the CLI refreshes tokens in place
[credentials.codex]
source = "directory"
path = "/var/lib/crucible/credentials/codex"
mount_mode = "rw-narrow"       # the CLI writes session and log state beside its auth file
[credentials.agy]
source = "directory"
path = "/var/lib/crucible/credentials/agy"
mount_mode = "ro"      # S1: AGY refreshes in memory per run from the mounted refresh token and
                               # wrote nothing back; ro holds until an authenticated C3 re-run shows
                               # a rotated refresh token, which would make it rw-narrow like Codex

[github.app]
app_id = 0                     # public identifier, not a secret
private_key_path = "/var/lib/crucible/credentials/github/app.pem"   # mounted read-only; never mounted into any worker
webhook_secret_path = "/var/lib/crucible/credentials/github/webhook.secret"
```

Subscription authentication is the requirement for harnesses: each harness
is logged in once by the operator (interactive login) **into Crucible's
own credential directory** (for example with `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, or the equivalent pointed at that directory), and that
directory is what gets mounted. It is never a copy of the operator's
daily-use directory: S1 showed Codex and AGY refresh their tokens on
their own during a run, and a refresh from a copy races the operator's
own session. No commercial API keys.

`rw-narrow` means: a per-attempt Docker volume seeded with **only the named
auth files** of that harness (the adapter's `credential_spec` lists them),
mounted writable at the paths the harness expects. Everything else the
harness reads from its config directory (settings, hooks, MCP definitions,
instruction files) is mounted read-only from a Crucible-owned template, so
a worker cannot plant a hook or a server definition that a later worker
inherits. On clean exit, Crucible validates each named auth file's JSON
shape and syncs back only those files, choosing by the newest issued-at
timestamp inside the token, never by exit order; then the volume is removed
at once. Per-harness concurrency is 1 whenever `rw-narrow` is in effect
(05b enforces this), because refresh tokens rotate and two concurrent
refreshes leave one worker with a revoked token and every later worker
locked out. Spike S1 records what each harness actually writes and where.

## GitHub App credentials

Locally the App private key and webhook secret are files under a private
credential directory mounted read-only into the `crucible` container only.
In Kubernetes they are a Secret mounted on the `crucible` pods only,
delivered through the GitOps repository as a SealedSecret (encrypted to
the cluster's key, so only the ciphertext is committed) or as an
ExternalSecret pointing at a secret store; Crucible reads a file path and
does not care which. Rotation is a new App key (GitHub allows several per
App), a new sealed manifest, and a revoke of the old key. Rebuilding a
cluster re-seals and is a natural rotation point. Workers, collectors,
verifiers, and publishers never mount that Secret. Crucible signs a JWT with the key in memory,
exchanges it for an installation token scoped to the one repository the
job needs, and hands that token to the publisher container as a file on
tmpfs read by a git credential helper. The token is never in `env`, `ps`,
a log, an event payload, or a database row. Its expiry and the job ID are
recorded; its value is not.

## Logs and artifacts

Provider log capture passes through a redaction filter with the same
patterns as the secret scanner plus the known shape of each harness's
tokens and of GitHub installation tokens. Redaction is best-effort defense
in depth, not the control: the control is that no secret value is ever
placed where a worker would print it, except its own harness credential,
which lives in a file it has no reason to cat.

## Remaining risks (confirmed in spikes, tracked in 22)

- A harness that logs its own token on auth failure. Mitigated by redaction
  and by the file-not-env rule.
- Token refresh races when two workers of the same harness run concurrently
  (why per-harness concurrency is 1).
- A harness that writes auth state outside its config directory (S1).
- A worker misusing its own harness credential within the egress allowlist
  (above; broker is the later answer).
- Host-process provider bypasses all of this; it is documented as insecure
  and requires explicit policy authorization.
