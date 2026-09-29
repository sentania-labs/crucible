# 06. Injected worker identity (WorkerIdentityV1)

Crucible assembles a per-attempt identity bundle at launch, mounts it
read-only into the worker, and never writes it into the repository.

## Bundle layout (mounted at `/crucible/identity`)

```
identity/
  IDENTITY.md          rendered role, objective, boundaries, reporting protocol
  contract.yaml        the TaskContractV1 verbatim
  contract.json        the same document as JSON (worker images have jq, no YAML parser)
  contract.sha256
  project/             copies or references of project_instructions entries
  skills/<name>/       only the skills the contract names
  policy.md            the deterministic policy in words: timeouts, paths, gates
  report-schema.json   CompletionClaimV1 JSON schema
  hooks/commit-msg     Crucible's commit hook, the one executable file: adds the
                       `<commit_trailer>: <external_id>` trailer to every commit,
                       as a courtesy nothing checks
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
   matching `CompletionClaimV1`, with `schema_version: "1.0"` (the format
   version, not the schema's name, hades #181); write `progress.jsonl`
   lines as milestones pass; capture each verification command's output to a log file there;
   write `blocked.md` and exit 75 to escalate; exit 0 only after
   `report.yaml` exists. Every path inside the report resolves against this
   directory. It lists the contract's acceptance criteria and says that
   `acceptance_mapping` has one entry per criterion, keyed by the criterion's
   id and never by a verification id, with a one-line example of each
   accepted form (a list, or a mapping keyed by id). It names the judgement
   fields the worker writes and the facts Crucible fills itself
   (`task_external_id`, `changed_files`, `refs`, `checks`, `run_evidence`),
   which the worker may leave out; a value it does write is only compared
   (hades #187, #215). It tells the worker to run
   `crucible-report check /crucible/report/report.yaml` and fix every
   problem it prints before exiting 0, and to check against
   `report-schema.json` by hand in an image without the command.
8. **Exit codes.** 0 done with report, 75 blocked, 70 cannot proceed (bad
   environment), anything else is failure.
9. **Prohibitions.** No pushing at all (the checkout has no remote
   credential; Crucible pushes after verification), no force, no branch
   other than `work_branch`, no delegating, no writing anywhere but the
   checkout and `/crucible/report`, no claiming completion without
   evidence, no closing references to issues the contract did not name.
10. **Commits.** One line: "Commit your work on `work_branch`." It says
    nothing about the trailer, the author, hooks or `--no-verify`: none of
    them is the worker's concern, and nothing refuses a commit for them
    (operator decision, 2026-09-29, hades FDY-0143).

## The commit hook

The preparer sets the checkout's `core.hooksPath` to
`/crucible/identity/hooks`, so the only hook that runs on a worker's commit
is Crucible's own text, mounted read-only and covered by the bundle hash; the
repository's own hooks never run (hades FDY-0135). The hook runs `git
interpret-trailers --if-exists doNothing --if-missing add` on the message
file and nothing else: no network, no repository content. A trailer with the
same key already in the message is kept as it is, so an amend, a harness
that writes the trailer itself, or a second run never adds a duplicate.
`--no-divider` keeps a `---` line in a body from being read as the start of
a patch. Any harness that commits through the git command line gets the
hook.

The trailer is a courtesy, not a requirement. On 2026-09-29 the operator
decided that the commit trailer stops being required and the task record is
the paper trail (hades FDY-0143): nothing checks the trailer, neither the
`commit_policy` gate (11) nor the publisher (23), and a commit made with
`--no-verify`, through a git library that runs no hooks, or by cherry-pick
is published like any other. The task view (04) records the work branch,
the pushed head, the pull request, and the merge commit and who merged.

## Harness-specific delivery

The bundle is the same for every harness. How the harness is pointed at it
differs (07): all harnesses use the single generated, untracked `AGENTS.md`
shim when the checkout has no applicable project instruction file. Claude Code
uses the existing project `CLAUDE.md` when one is present, so Crucible writes
no `AGENTS.md` shim in that case. Codex gets `IDENTITY.md` on stdin ahead of
the prompt; AGY gets `--add-dir /crucible/identity` and a short argv prompt
that says to read `IDENTITY.md` first.

Shims are written by Crucible after checkout, listed in
`.git/info/exclude`, and their absence from the diff is a gate (11).

## What is never in the bundle

Credentials, the Crucible API token, GitHub tokens, other tasks'
contracts, external review feedback that Foundry has not turned into a
correction contract, the orchestrator's own identity, or any file the
contract did not name.
