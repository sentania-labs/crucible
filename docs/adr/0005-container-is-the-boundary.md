# ADR 0005: The execution environment is the security boundary; harness sandboxes are defense in depth

Status: proposed, 2026-09-16.

## Context

Codex's built-in sandbox (bubblewrap) needs user namespaces, which some
hosts deny, so it cannot be assumed available. Claude Code and AGY run
with permission prompts bypassed in non-interactive mode. Relying on any
harness's internal sandbox would make Crucible's safety depend on three
vendors' implementations and on host kernel settings.

## Decision

Crucible's execution provider provides the primary boundary: non-root
user, all capabilities dropped, no new privileges, read-only root, tmpfs
scratch, explicit mounts only, resource limits, private network with
egress policy, no socket, no control-plane credentials. Harness sandboxes
are enabled when they work (S2) and never required. The operator's
assumption is confirmed.

## Remaining risks

Container escape via kernel vulnerability (mitigated by no capabilities and
seccomp default profile; microVM isolation deferred). Credential refresh
races (S1). A harness logging its own token (redaction plus file-not-env).
Host-process provider bypasses everything and is documented as such.
