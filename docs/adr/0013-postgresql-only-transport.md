# ADR 0013: PostgreSQL is the only transport; no Redis or broker until measured need

Status: accepted, operator checkpoint 2026-09-16.

## Context

Events, wakes, leases, and queued work could use a broker. At Crucible's
scale (a handful of concurrent workers, one supervisor) that adds
infrastructure, failure modes, and a second source of truth.

## Decision

PostgreSQL holds authoritative state, durable events, wakes, leases, and
queued work. The supervisor polls, claims transactionally with `FOR UPDATE
SKIP LOCKED`, and fences writes with database-backed leases. No Redis,
NATS, or other transport is introduced. If measurements show PostgreSQL
cannot meet event-delivery latency or queue throughput, the alternative is
evaluated in a new ADR with those numbers.

## Consequences

Wake delivery latency is bounded by the tick interval, which is acceptable
because Foundry polls on start-of-session anyway. Queue depth and tick
duration are exposed on `/supervisor` so the measurement exists before any
decision to change transport.
