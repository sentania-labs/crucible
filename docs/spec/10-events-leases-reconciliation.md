# 10. Events, leases, heartbeats, and reconciliation

## Events (EventV1)

Append-only table `events` with a global monotonic `seq` (BIGSERIAL), `ts`,
`kind`, `task_id`, `execution_id`, `attempt_id`, `principal` (who caused it:
`crucible`, an API principal, or `worker:<attempt>`), `payload` (JSONB,
schema per kind), and `verified` (false for anything a worker asserted).
Kinds are an enum; adding one is a migration. Events are never updated or
deleted. Retention: forever in v0.x (volume is small); archival to object
storage is a later policy.

The API exposes events per task and a global feed with cursor. Log lines
are not events; they are a separate stream (below).

## Logs (LogStream)

`log_chunks`: `attempt_id`, `stream`, `offset_start`, `offset_end`, `ts`,
`content` (bytea, gzip above a threshold). The supervisor pulls provider
logs each tick and appends. Resume position is the last stored
`(timestamp, sha256(line))`: the pull asks the provider for lines since
that timestamp and skips until the hash matches, which avoids both
duplicates and drops among lines sharing a timestamp (Docker has no byte
offsets). Live tail streams chunks as they land. Log bytes advancing is one
heartbeat signal. An attempt records `logs_drained` after the final pull
following exit; cleanup never runs before it.

## Leases

| Lease | Held by | Renewed | Expiry meaning |
|---|---|---|---|
| supervisor | one Crucible instance | every tick (default 5 s), TTL 30 s | another instance may take over; the old one must stop acting on expiry |
| attempt | the supervisor on behalf of a running attempt | every observation tick | informational: an expired attempt lease means observation stopped (Crucible was down); loss is decided only when the provider cannot see the worker |
| checkout | an attempt, for `repository.url` + `work_branch` | for the life of the attempt | released on terminal attempt state or by reconcile after loss |

Leases are rows with `holder`, `expires_at`, `fenced_token` (monotonic).
Every write the supervisor makes carries its fenced token; a write with a
stale token is rejected by a trigger. This is what stops a paused-then-resumed
old supervisor from corrupting state after a takeover.

## Heartbeats

`heartbeats`: `attempt_id`, `ts`, `signal` (container_running, log_advanced,
fs_changed, progress_line), `detail`. The supervisor derives worker state:
any signal within `stall_warn_seconds` is `alive`; none within
`stall_fail_seconds` is `stalled`. Defaults: 300 s warn, 1800 s fail,
overridable per policy. A worker that emits progress lines but changes
nothing for the fail window is still stalled; progress lines are unverified.

## Timeouts

`timeout_seconds` from the contract, bounded by policy. On expiry: `drain`
(SIGTERM, wait `grace_seconds`, default 60), then `kill`, collect whatever
exists, attempt `timed_out`. The report gate then fails unless a valid report
was written before the signal.

## Reconciliation

Runs at supervisor start and every `reconcile_interval` (default 60 s):

1. Take or verify the supervisor lease. If not held, do nothing.
2. For every attempt in a non-terminal state, ask the provider to observe.
   - Provider sees it running: renew attempt lease, pull logs, record
     heartbeat signals.
   - Provider sees it exited: run the exit path as if the tick had caught
     it live.
   - Provider cannot see it: mark `lost`, record an event with the last
     known observation, decide retry per policy.
3. For every provider handle with a Crucible label but no live attempt row:
   orphan; terminate and clean up; event recorded.
4. For every checkout lease past expiry with no live attempt: release.
5. For every task in `reported` with pending gates: evaluate.
6. For every wake undelivered past its retry schedule: redeliver.
7. Write the supervisor liveness row (`last_tick`, duration, counts).

Reconciliation is idempotent; running it twice changes nothing the second
time. That property is tested.

## Foundry disconnect

Nothing above references an orchestrator session. Foundry's absence only
means wakes accumulate for poll. That is the whole mechanism for "continue
while disconnected," and it is tested by killing the API client mid-run.
