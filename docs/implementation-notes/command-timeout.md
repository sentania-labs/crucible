# Command timeout from the launch (issue 128, FDY-0122)

## Decision

The operator, 2026-09-25 at 2:23 PM: "in the worker pods we should do this (or
it's equivilant) for all harnesess, and the timeout should be set by the task
launch. i.e. configurable at a hades level or dispatched at run time."

Implemented as a policy limit, `limits.command_timeout_ms` {min, max, default},
default 3,600,000 ms (60 minutes). A contract may set any value within the
policy's bounds with `execution_request.command_timeout_ms` (by design, like
`timeout_seconds`, it is not held under the default), and a launch never sets
it above the attempt's `timeout_seconds`. It is edited in place from the admin
API, CLI and UI; each save writes a new policy version. Existing policy
versions were not rewritten: one without the field takes the default bounds.

`incomplete` exits 0, so nothing downstream may treat a zero exit code alone as
success (Codex round on PR 151, 2026-09-25). The pre-PR `exit_clean` gate needs
exit code 0 and class `completed` or `completed_without_report`, and a review
execution's report is recorded, and the execution succeeds, only on that same
clean exit. Foundry ruled the same day that a contract value above the policy
default is by design, as long as it stays within the policy's min and max.

## What each harness does, and the evidence

Every fact below comes from the pinned worker image
`crucible-worker:20260916-d39e5748bf08` (Claude Code 2.1.280, Codex 0.156.0,
AGY 1.2.8, Hermes 0.19.0), run on 2026-09-25 under `--network none` against
`tests/e2e/stub_model.py`. No harness login was used. The table and the launch
settings are in 07; this note keeps the observations.

- **Claude Code.** With `BASH_DEFAULT_TIMEOUT_MS=3000` and no other setting, a
  40 s `python3 -c "import time; time.sleep(40)"` was answered "Command did not
  complete within its 3s timeout and was moved to the background"; `claude -p`
  exited 0 nine seconds in and the command never finished. The stream-json
  transcript recorded `task_started` (`is_backgrounded: true`), then after the
  final `result`, `task_updated` `killed` and `task_notification` `stopped`. A
  6 s command was backgrounded too, but finished before the CLI exited, and the
  transcript recorded `completed`. A command starting with `sleep` is never
  auto-backgrounded (the binary's own exclusion list is `["sleep"]`), which is why
  the first attempt at a reproduction did not show the trap. With
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` the same 40 s command was ended at 3 s
  ("Exit code 143 / Command timed out after 3s") and nothing was left running.
  The documented setting (https://code.claude.com/docs/en/env-vars) describes
  subagents; that it also covers Bash timeout backgrounding was observed here,
  not read.
- **Codex.** Its tool is `exec_command` (unified exec), whose `yield_time_ms` is
  "effective range 250-30000 ms". A 40 s command came back after 10 s as "Process
  running with session ID"; when the model ended its turn Codex exited 0 and the
  command never finished, leaving an `item.started` `command_execution` with no
  `item.completed`. `--disable unified_exec` and `-c features.unified_exec=false`
  did not change the tool: `codex debug models` shows the catalog's own
  `shell_type: unified_exec` for the models, so nothing at the launch makes a
  command block. When the model polled with `write_stdin` asking for 300 s, each
  poll lasted at most `background_terminal_max_timeout` (8 s when set to 8000),
  and the command completed; that key is documented at
  https://learn.chatgpt.com/docs/config-file/config-reference.
- **Hermes.** From its source in the image (`tools/terminal_tool.py`): a
  foreground command is killed at `TERMINAL_TIMEOUT` (default 180 s) and a model
  may not ask above `TERMINAL_MAX_FOREGROUND_TIMEOUT` (default 600 s). Observed: at
  3 s the command was reported "[Command timed out after 3s]", exit 124; at 20 s
  an 8 s command completed. A model-requested `background=true` under `-z` got
  "notify_on_complete / watch_patterns are not available in this session", Hermes
  exited 0 two seconds in with `completed: true`, and its `processes.json` still
  listed the command. The registry rewrites that file whenever a process ends
  (`_move_to_finished`), so an entry at exit is a command still running.
- **AGY.** `agy --help` has `--print-timeout` and nothing for commands. The
  binary's strings show `run_command` taking `Blocking` and `WaitMsBeforeAsync`
  from the model, and "Background command is still running after %ds". The
  public CLI documentation pages (antigravity.google/docs) name no setting for
  either. AGY needs a Google login before any turn (its adapter's endpoints), so
  it was not run and its print-mode behaviour at exit is unknown.

## Known limits

- The stall limits apply to a silent command only where the harness gives no live
  evidence of it (issue 152, below): AGY, and a Hermes foreground command. There a
  command outlasting `stall_fail_seconds` (1800 s in the seeded policy) is a stall
  before it reaches the 60-minute default.
- A worker that leaves a server or other long command running when it ends its
  turn is `incomplete`, even with a valid report. That is the requirement read
  literally; `incomplete` is not retried.
- The transcript is evidence only when it is collected: a report directory file
  over the policy's size cap is dropped at collection, and with it the Claude Code
  or Codex record of what was running.
- AGY has only a prompt instruction (07), not a setting, and no evidence is read.

## Why no generic process check

A launch-wrapper check for any process alive after the harness exits was
considered and not built: a build that leaves a daemon behind (a Gradle daemon,
a git fsmonitor) would mark every such attempt incomplete. The requirement is
what the harness's own tooling reports, so the evidence is per harness.

## A command in flight pauses the stall clock (issue 152, FDY-0123)

The operator, 2026-09-27: "a running command counts as activity. While a harness
reports a command in flight, the stall clock pauses, so the stall limit applies to a
worker doing nothing and the command timeout applies to a command running long."
Foundry issued the work the same day on the operator's "apply your recommendation".

The evidence is the one this note already reads at exit, read from the live log
instead: each adapter's `command_tracker()` is fed every stored log chunk in order,
and while it reports a command the supervisor writes a `command_running` activity
heartbeat whenever the newest activity is older than a minute (or half the shorter
stall limit, when that is less). Both stall clocks (warn and fail) read activity, so
both pause; when the command ends they run again from within that window of its end.
A command still reported a minute past its command timeout stops counting: a Codex
session and a Hermes background process are not ended by the harness's own timeout,
and the operator's rule is that the command timeout bounds a command. The tracker is
rebuilt from the stored log after a supervisor restart or takeover.

The non-author review round (2026-09-27) raised the short-limit renewal and the
command-timeout bound; both were adopted as described above.

What was seen on 2026-09-27, pinned image `crucible-worker:20260916-d39e5748bf08`,
stub model, `--network none`, the log read every half second while a 15 s silent
command ran:

- **Claude Code** wrote the `assistant` event with the `tool_use` block before the
  command started and the `user` event with its `tool_result` when it ended; the
  tracker reported `tool Bash: python3 -c ...` for at least 10 of the 15 seconds and
  nothing after.
- **Codex** wrote `item.started` for the `command_execution` item when the command
  started and `item.completed` only after the model's `write_stdin` polls saw it end,
  so the item is open across the polls.
- **Hermes** under `-z` wrote nothing to stdout or stderr while it worked: its
  one-shot mode sends both to `/dev/null` (`hermes_cli/oneshot.py` in the image), and a
  foreground command is run directly, never through the process registry
  (`tools/terminal_tool.py`: only `background=true` calls `process_registry.spawn_*`).
  The launch wrapper therefore counts the registry's `"session_id"` entries every 10
  seconds while Hermes runs and writes `crucible-launch: commands running: <n>` to
  stderr on each change. With a background command and a model slow to answer
  afterwards, the count `1` reached the log during the run. A 12 s foreground command
  produced no evidence at all, which is the limit 05b states.
- **AGY** was not run (no Google login) and has no tracker.

Known limits of this change:

- A Hermes foreground command, the usual kind, is still counted as silence; so is any
  AGY command. Hermes ends a foreground command at `TERMINAL_TIMEOUT` (the launch's
  command timeout), so an operator who wants long silent Hermes commands to survive
  needs a `stall_fail_seconds` above the command timeout.
- The evidence is the harness's own stream, which the worker's process writes. A
  worker could keep its own stall clock paused by writing such lines, as it could
  already by writing any output; `timeout_seconds` still ends the attempt.
- A Claude Code `tool_result` line longer than 32 MiB is dropped by the tracker; the
  call it answers stays open until the model's next message, which closes it.
- The first observation after a supervisor restart reads the attempt's whole stored
  log once, 500 chunks at a time. A command's age is counted from when this
  supervisor first saw it, so after a restart a command can hold the stall clock for
  up to one more command timeout.
- Hermes's count is one entry: when it changes, its age starts again.
- The pause can be no finer than the supervisor's tick (5 seconds): a stall limit
  shorter than two ticks can still pass during a command.
