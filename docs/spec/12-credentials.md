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
   read, referenced by name (`credential:codex`) in configuration. The one
   onboarding exception is the Hermes key paste: its value exists only in the
   TLS-protected admin request and the immediate mode-0600 file write (on
   Kubernetes, the request that writes the service-owned Secret, ADR 0015). It is
   never returned, stored in the database, logged, or copied into an audit event.
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
  sync and on every path that skips it (16);
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

On Kubernetes the credential is the harness Secret in `crucible-workers`, and
the service is its only writer (ADR 0015, the operator's decisions of
2026-09-23): it creates the Secret when absent, labels it as its own, and writes
it from the login Job (25), the Hermes key entry, and the sync-back below. GitOps
does not deliver it, because a Secret with two writers drifts and a sync would
restore a token the harness has already rotated. A login's files reach the
Secret only after they pass the shape check, over exec and never through a Pod
log, which the kubelet writes to the node's disk.

The credential root and the per-harness directory under it are created by
the operator or by the deployment script, owned by the Crucible service
user, mode 0700, before the stack starts (13). Crucible does not create
them, and it refuses at startup when a configured credential directory is
not owned by the service user or is not 0700.

An administrative record never carries a credential value, and the audit is
served back through the API, so the reason string and the whole assembled
event payload of every administrative mutation are scanned before they are
written. A secret-shaped reason is refused with the pattern named and the
value never quoted, rather than being stored or quietly redacted: an
operator who pasted a token where a sentence belonged needs to know it
landed nowhere, because what they do next is revoke it (25).

Removing or retiring a credential directory means shredding it: a zero
overwrite of every regular file, then removal. On a copy-on-write filesystem
that is best effort about whether the old bytes are really gone, and that
limit is accepted. It is not best effort about whether it finished. A
directory it cannot read, an entry that is neither a regular file, a symlink
nor a directory (a socket or a fifo), a directory that is not empty when it
is reached, and a file the harness CLI writes in the middle of the walk must
none of them leave data behind silently. The walk is bottom-up, records
rather than stops at the first thing it cannot remove, runs a second pass so
a file written mid-walk is absorbed, and raises naming exactly what remains.
The retention sweep records such a failure and carries on to the other
directories.

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
mount_mode = "rw-narrow"       # the first Crucible-side run past the one-hour expiry rotated the
                               # token and the copy carried a newer expiry, which is the evidence
                               # S1 left open; the token file syncs back by that field

[credentials.hermes]
source = "directory"
path = "/var/lib/crucible/credentials/hermes"         # contains only api-key
mount_mode = "ro"                # the gateway key never refreshes and never syncs back

[github.app]
app_id = 0                     # public identifier, not a secret
private_key_path = "/var/lib/crucible/credentials/github/app.pem"   # mounted read-only; never mounted into any worker
webhook_secret_path = "/var/lib/crucible/credentials/github/webhook.secret"
```

Every `path` above is a directory that already exists when Crucible starts,
owned by the service user and mode 0700 (13).

Subscription authentication is the requirement for harnesses: each harness
is logged in once by the operator (interactive login) **into Crucible's
own credential directory** (for example with `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, or the equivalent pointed at that directory), and that
directory is what gets mounted. It is never a copy of the operator's
daily-use directory: S1 showed Codex and AGY refresh their tokens on
their own during a run, and a refresh from a copy races the operator's
own session. No commercial API keys.

Hermes is the narrow API-key exception. The admin UI and
`crucible-admin credentials set --harness hermes` read the value from a password
field or hidden stdin prompt, atomically replace `api-key` with mode 0600, and record
only `credential set`. Validation first calls `/health/readiness` without a bearer,
then calls `/v1/models` with the bearer. A 200 validates the key, a 401 is an
authentication failure, and other responses are inconclusive. Workers receive only a
read-only per-attempt copy. Kubernetes stores the same one-file shape in the
`crucible-harness-hermes` Secret, which the same key entry (also on the Routing
page) writes, and every surface reports only whether a key is set.

`rw-narrow` means: a per-attempt copy holding **only the named auth files**
of that harness (the adapter's `credential_spec` lists them), owned by the
worker's uid, mounted writable at the paths the harness expects. The copy's
directory is mode 0700 and each auth file inside it is mode 0600, so on the
host the whole copy belongs to a subordinate uid no other user can read. The requirement is the copy's properties, not its mechanism: only
the named files, readable by no other user, never shared between attempts,
and removed as soon as the sync-back is done. The Docker provider makes it a
per-attempt directory in the attempt's workspace rather than a named volume,
because a named volume cannot be removed while the worker container still
exists and the worker has to outlive the run until its logs are drained (08).

The value never travels through a mount the Crucible process reads. The
provider seeds the copy through the daemon's container archive endpoint into
the created, not yet started, worker, and reads it back through the same
endpoint after the worker exits; the bytes are in the request body and
nowhere else, not in `Env`, not in `Cmd`, not in a bind source, and not in
Crucible's own argv. Everything else the
harness reads from its config directory (settings, hooks, MCP definitions,
instruction files) is mounted read-only from a Crucible-owned template, so
a worker cannot plant a hook or a server definition that a later worker
inherits. On clean exit, Crucible validates each named auth file's JSON
shape and syncs back only those files, choosing by the newest issued-at
timestamp inside the token, never by exit order; a file that is not the
expected shape, or not newer than the source as it stands, is recorded and
not written. A file the adapter marks as state rather than a credential is
seeded and never written back. Then the copy is removed
at once.

The copy is removed on **every** path, not only the clean one: a start that
failed after seeding, a worker the daemon lost, and a transport failure
during the read-back all remove it, the last from a `finally` path so a
repeatedly failing read-back cannot leave it on disk for a retry to find.
Cleanup removes it under every retention policy, `keep` included, so 08's
"keep or delete the workspace per policy" never keeps the credential copy.
A harness counts against its concurrency cap until its copy has been synced
back and removed, which is after the attempt is `exited`: a second seeding
from the source before that is the refresh race below. Per-harness concurrency is 1 whenever `rw-narrow` is in effect
(05b enforces this), because refresh tokens rotate and two concurrent
refreshes leave one worker with a revoked token and every later worker
locked out. Spike S1 records what each harness actually writes and where.

A login is the same race from the other side: it replaces the credential, and
an attempt holding a copy of the one it replaced would sync a refresh of a
superseded session back over it. So a login refuses while any attempt of the
harness holds the credential (the same states the cap counts), a launch of the
harness waits while a login for it runs, and on Kubernetes the login checks
again at the moment it writes the Secret (an attempt or a credential probe that
came to hold a copy while it ran means the new files are not stored), and a
probe refuses while a login Job for the harness exists.

## GitHub App credentials

The service owns the App credential (ADR 0016, crucible#120): the operator
connects an existing App by its id and one of its private keys on the GitHub
page (or `POST /v1/admin/github/app`, `crucible admin github connect`), the
service checks them with GitHub's `GET /app` before it stores anything, and
from then on it is the credential's only writer. In Kubernetes it is the
Secret `crucible-github-app` in the `crucible` namespace, keys `app-id`,
`app.pem` and `webhook.secret`, created and labelled by the service and read
through the API server on each signature; it is also mounted, optional, on
the `crucible` pods only, where the webhook route reads `webhook.secret`.
GitOps does not deliver it. Locally the same three files sit beside
`github.app.private_key_path` in a private directory of the `crucible`
container, written mode 0600 by the same flow. A Secret or directory a
deployment filled itself still works when `github.enabled` and
`github.app.app_id` name it. Rotation is a new App key (GitHub allows several
per App), a Connect GitHub with it, and a revoke of the old key in GitHub.
Workers, collectors, verifiers, and publishers never mount that Secret. Crucible signs a JWT with the key in memory,
exchanges it for an installation token scoped to the one repository the
job needs, and hands that token to the publisher container as a file on
tmpfs read by a git credential helper. The token is never in `env`, `ps`,
a log, an event payload, or a database row. Its expiry and the job ID are
recorded; its value is not.

## Logs and artifacts

Provider log capture passes through a redaction filter with the same
patterns as the secret scanner plus the known shape of each harness's
tokens and of GitHub installation tokens. Worker log chunks are redacted
before they are stored, not on the way out; the resume position keeps the
hash of the raw line, which is what the provider's stream is compared
against (10). The harness token shapes the scanner knows include Codex's
refresh token, which is not a JWT: two short base64url segments and one
long one. `.gitleaks.toml` extends the scanner's default rules with every
pattern the scanner carries, so `make scan` and the CI scan job catch the
same shapes, and a test holds the two lists equal. The installation-token pattern is
the literal `ghs_` followed by at least 20 characters from
`[A-Za-z0-9._-]`, with no upper bound: the real value is about 390
characters and contains dots, so the fixed-length 40-character form most
published patterns assume matches nothing (S10).

Redaction is not only a log filter. Everything a repository controls is
redacted at the point Crucible reads it, before it reaches a stored row: a
CI log excerpt fetched through the Actions read permission, the publisher
container's own output, the remote's refusal message on a failed push, and
every review body, comment body, and PR title arriving by poll or webhook
(23). Redaction is best-effort defense
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
