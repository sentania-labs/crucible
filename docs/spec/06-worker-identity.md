# 06. Injected worker identity (WorkerIdentityV1)

Crucible assembles a per-attempt identity bundle at launch, mounts it
read-only into the worker, and never writes it into the repository.

## Bundle layout (mounted at `/crucible/identity`)

```
identity/
  IDENTITY.md          rendered role, objective, boundaries, reporting protocol
  contract.yaml        the TaskContractV1 verbatim
  contract.sha256
  project/             copies or references of project_instructions entries
  skills/<name>/       only the skills the contract names
  policy.md            the deterministic policy in words: timeouts, paths, gates
  report-schema.json   CompletionClaimV1 JSON schema
  history/             prior attempts' reports, open and answered escalations with
                       verbatim decisions, and the latest AcceptanceResult reasoning
                       (present on retries, after a decision, and on needs_more_work)
```

Skills named in `project_instructions` are copied from the configured
`skills_root`; only the named ones, nothing else in that root.

The bundle's content hash is recorded on the attempt. The rendered
`IDENTITY.md` is stored as an artifact so the exact instructions a worker saw
are reproducible.

## IDENTITY.md sections (rendered from templates in `crucible/adapters/harness/templates/`)

1. **Role.** "You are a worker executing task `<external_id>` for
   `<orchestrator principal>`. You implement; you do not redefine the task."
2. **Objective.** From the contract.
3. **Authority and boundaries.** Allowed and prohibited paths, prohibited
   actions, dependency and CI rules, network mode, branch name, the rule that
   nothing outside the checkout is yours.
4. **Instruction precedence.** Operator's explicit words, safety and
   repository protection, this contract, project docs and skills, the
   delivery policy, global skills, harness defaults. On material conflict:
   stop and escalate.
5. **Procedure pointers.** Which project files and skills to read first.
6. **Verification.** The `required_verification` list, verbatim, and the
   instruction to capture each command's output to `report/`.
7. **Reporting protocol.** The report directory is `/crucible/report`,
   mounted writable and outside the checkout. Write `report.yaml` there
   matching `CompletionClaimV1`; write `progress.jsonl` lines as milestones
   pass; capture each verification command's output to a log file there;
   write `blocked.md` and exit 75 to escalate; exit 0 only after
   `report.yaml` exists. Every path inside the report resolves against this
   directory.
8. **Exit codes.** 0 done with report, 75 blocked, 70 cannot proceed (bad
   environment), anything else is failure.
9. **Prohibitions.** No pushing to any branch other than `work_branch`, no
   force, no delegating, no writing anywhere but the checkout and
   `/crucible/report`, no claiming completion without evidence.

## Harness-specific delivery

The bundle is the same for every harness. How the harness is pointed at it
differs (07): Claude Code gets it as an appended system prompt file plus a
generated, untracked `CLAUDE.md` shim in the checkout if the project has
none; Codex gets `IDENTITY.md` on stdin ahead of the prompt plus an untracked
`AGENTS.md` shim when needed; AGY gets `--add-dir /crucible/identity` and a
short argv prompt that says to read `IDENTITY.md` first.

Shims are written by Crucible after checkout, listed in
`.git/info/exclude`, and their absence from the diff is a gate (11).

## What is never in the bundle

Credentials, the Crucible API token, other tasks' contracts, the
orchestrator's own identity, or any file the contract did not name.
