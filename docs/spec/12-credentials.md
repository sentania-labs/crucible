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
mount_mode = "rw-narrow"       # the CLI writes session and log state beside its auth file
[credentials.agy]
source = "directory"
path = "/var/lib/crucible/credentials/agy"
mount_mode = "ro"
[credentials.github-app]
source = "file"
path = "/var/lib/crucible/credentials/github/token"
mount_mode = "ro"
mount_path = "/crucible/credentials/github/token"   # read by the git credential helper, never exported to env
```

Subscription authentication is the requirement: each harness is logged in
once on the host by the operator (interactive login), and the resulting
state directory is what gets mounted. No commercial API keys. Where a
harness offers a long-lived subscription token command (Claude Code's
`setup-token` flow, for example) that token file is the credential.

`rw-narrow` means: a per-attempt Docker volume seeded with **only the named
auth files** of that harness (the adapter's `credential_spec` lists them,
for example an OAuth credentials file and, for Claude Code, the top-level
state file it keeps beside its config directory), mounted writable at the
paths the harness expects. Everything else the harness reads from its config
directory (settings, hooks, MCP definitions, instruction files) is mounted
read-only from a Crucible-owned template, so a worker cannot plant a hook or
a server definition that a later worker inherits. On clean exit, Crucible
validates each named auth file's JSON shape and syncs back only those files,
choosing by the newest issued-at timestamp inside the token, never by exit
order. Per-harness concurrency is 1 whenever `rw-narrow` is in effect (05b
enforces this), because refresh tokens rotate and two concurrent refreshes
leave one worker with a revoked token and every later worker locked out.
Spike S1 records what each harness actually writes and where.

## Repository credentials

A GitHub App installation token (or a fine-grained token, operator's
choice, 22 Q2) scoped to the target repository, mounted read-only as a file.
It is never placed in the environment. The provider installs a git
credential helper in the worker image that reads the mounted file, and a
`gh` configuration that does the same, so `git push` and `gh pr create`
work without the token ever appearing in `env`, `ps`, or logs. Commit
author identity comes from policy (`git.author_name`, `git.author_email`),
set through the worker's read-only git config template, with the attempt ID
as a commit trailer. Workers push to `work_branch` only; a token cannot
enforce a branch, so `branch_pushed_at_head` and repository branch
protection are the guard. Force-push is prohibited by identity and detected
by the gate.

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
