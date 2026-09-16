# ADR 0008: External review is a bounded quality input, not a consensus loop

Status: accepted, operator decisions 3, 4, and 5, 2026-09-16.

## Context

An external reviewer bot on a PR can comment on every push. Treating each
comment as a mandatory correction produces unbounded rounds and hands
judgment to a party outside the boundary.

## Decision

- Internal non-author review is required before any PR; Foundry decides
  when and what; Crucible runs it as a `review` execution when asked or
  records an uploaded report.
- External review is configured per repository policy. Default: one
  round, no retrigger after correction, no requirement on the final SHA,
  a recorded disposition for every comment.
- A round counts only from an allowlisted reviewer login. Any other
  activity is recorded and satisfies nothing.
- Crucible records feedback and wakes Foundry. Foundry interprets and
  records dispositions. Accepted feedback may create one correction
  execution as a new contract version Foundry authors; Crucible never
  forwards feedback to a worker.
- The corrected head reruns every required test, scan, verification
  command, and mechanical pre-PR gate. It does not automatically get a
  second internal review (Foundry may request one) and never a second
  external review under the default policy. Final CI must be green; a CI
  failure escalates.
- A round is counted only from an allowlisted login, including its
  thumbs-up reaction meaning "no findings". Advancement waits for the
  configured number of rounds.
- Crucible watches the PR by webhook plus polling; Foundry keeps no loop.

## Consequences

Some external findings will be declined with a recorded reason rather
than fixed, which is the intent. Repositories wanting more rounds set the
policy. A change to the reviewer bot's login or signal shape is a policy
change and an S12 rerun, not code.
