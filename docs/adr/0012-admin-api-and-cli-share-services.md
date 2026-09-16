# ADR 0012: Administration is a versioned admin API and a CLI on the same application services; no web UI before readiness

Status: accepted, operator checkpoint 2026-09-16.

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
drive both. Interactive logins need an operator present; the CLI supports
device-code flows so that can happen from another machine. Foundry gets a
read-only capabilities view and nothing more.
