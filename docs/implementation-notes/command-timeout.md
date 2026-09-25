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

- The stall limits still apply (05b): a silent command outlasting
  `stall_fail_seconds` (1800 s in the seeded policy) is a stall before it reaches
  the 60-minute default.
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
