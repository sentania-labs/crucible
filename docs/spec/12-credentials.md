# 12. Credential and secret handling

## Principles

1. Crucible never stores a credential. Not in PostgreSQL, contracts, images,
   git, logs, artifacts, or reports. Secret scanning runs on every artifact
   and report before storage, and on every contract at submit.
2. One worker, one harness, one credential set. A Codex worker cannot see
   Claude Code's OAuth state, and vice versa.
3. Credentials reach a worker only as a mount from a source Crucible can
   read, referenced by name (`credential:codex`) in configuration, never
   by value in any API body.
4. Read-only by default. A narrow writable volume only when a harness must
   refresh its own auth state, and then only that directory.
5. The Crucible API token, database URL, and Docker proxy endpoint are never
   mounted into a worker. Verified by an integration test that greps the
   worker's environment and filesystem.

## Credential sources (config, sanitized example in `examples/config/`)

```toml
[credentials.claude_code]
source = "directory"
path = "/var/lib/crucible/credentials/claude_code"   # contains the CLI's auth state
mount_mode = "rw-narrow"       # the CLI refreshes tokens in place
[credentials.codex]
source = "directory"
path = "/var/lib/crucible/credentials/codex"
mount_mode = "ro"              # promoted to rw-narrow if spike S1 shows refresh writes
[credentials.agy]
source = "directory"
path = "/var/lib/crucible/credentials/agy"
mount_mode = "ro"
[credentials.github-app]
source = "file"
path = "/var/lib/crucible/credentials/github/token"
mount_mode = "ro"
inject_as = "GH_TOKEN"         # env var name only; the provider reads the file at launch
```

Subscription authentication is the requirement: each harness is logged in
once on the host by the operator (interactive login), and the resulting
state directory is what gets mounted. No commercial API keys. Where a
harness offers a long-lived subscription token command (Claude Code's
`setup-token` flow, for example) that token file is the credential.

`rw-narrow` means: a per-harness Docker volume seeded from the source
directory, mounted writable at the harness's config path only, synced back
to the source on clean exit if the harness rewrote it, never shared between
two concurrent workers of the same harness (each gets its own copy; the last
successful refresh wins on sync, with a recorded event). Spike S1 (21)
determines whether concurrent refresh breaks any harness, and if so,
per-harness concurrency drops to 1 until solved.

## Repository credentials

A GitHub token or App installation token, scoped to the target repository,
read-only mounted, injected as an environment variable by the provider from
the mounted file at container start. Workers push to `work_branch` only; the
token's permissions cannot enforce a branch, so `branch_pushed_at_head` and
branch protection on the repository are the guard. Force-push is prohibited
by identity and detectable by the gate (remote head must equal collected
HEAD).

## Logs and artifacts

Provider log capture passes through a redaction filter with the same
patterns as the secret scanner plus the known shape of each harness's
tokens. Redaction is best-effort defense in depth, not the control: the
control is that no secret value is ever placed where a worker would print
it, except its own harness credential, which lives in a file it has no
reason to cat.

## Remaining risks (to confirm in spikes and record in 22)

- A harness that logs its own token on auth failure. Mitigated by redaction
  and by the file-not-env rule.
- Token refresh races when two workers of the same harness run concurrently.
- A harness that writes auth state outside its config directory.
- Host-process provider bypasses all of this; it is documented as insecure.
