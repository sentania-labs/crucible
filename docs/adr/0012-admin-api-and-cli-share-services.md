# ADR 0012: Administration is a versioned admin API and a CLI on the same application services; no web UI before readiness

Status: accepted, operator checkpoint 2026-09-16; consequences amended
2026-09-17 with the login exception below (Foundry's amendment to the
recorded decision, from what C5b built; the decision itself is unchanged and
no new operator decision was taken).

## Context

Harness credentials, availability, image versions, providers, GitHub App
configuration, and health are Crucible concerns. Foundry must be able to
report an unavailable capability but must never hold a Crucible credential.
A web portal is wanted later and must not become the owner of state.

## Decision

`/v1/admin` (admin role) and `crucible-admin` are two thin clients of one
application service layer; every mutation is an audited event with a
reason; every response is sanitized (states, timestamps, versions, digests,
metadata hashes; never values). Credential onboarding is an administrative
workflow that logs the harness in directly into a dedicated Crucible
directory and validates with a bounded probe; the daily-session
compatibility test gates enabling. No web UI before the worker-supervision
readiness milestone; the API is designed so a later portal needs no other
data source and owns nothing.

## Consequences

Two entry points must stay behaviorally identical, enforced by tests that
drive both. That still binds every operation but one. Interactive logins
need an operator present; the CLI supports device-code flows so that can
happen from another machine. Foundry gets a read-only capabilities view and
nothing more.

The one exception, from C5b: credential login spawns the harness's own CLI
in a pty, and the Crucible service image carries none of the three CLIs, so
on a normal deployment the CLI form works where the CLI is installed and the
API form is unavailable and refuses with that reason. Both entry points
still call the same application service and refuse identically, so the
exception is about where the flow can run, not about divergent behaviour.
25, "Credential onboarding workflow", step 0, specifies it. The route to
closing it is to run the flow inside the promoted harness image, the way the
probe runs, with the pty and the operator's pasted code relayed through the
daemon's attach stream; until then this consequence has a named gap rather
than an unrecorded one.
