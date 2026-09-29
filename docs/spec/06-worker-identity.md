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
  history/             prior attempts' reports, open and answered escalations with
                       verbatim decisions, and the latest AcceptanceResult reasoning
                       (present on retries, after a decision, and on needs_more_work)
```

Skills named in `project_instructions` are copied from the configured
`skills_root`; only the named ones, nothing else in that root.

The bundle's content hash is recorded on the attempt. The rendered
`IDENTITY.md` is stored as an artifact so the exact instructions a worker saw
are reproducible.

## IDENTITY.md (rendered by `crucible/adapters/execution/identity.py`)

Short and plain: about 300 words for a typical contract (the operator's
direction of 2026-09-29, FDY-0140). Crucible re-runs every check, commits what
the worker leaves uncommitted, and enforces the scope and the gates itself, so
the worker is told what to do, not how Crucible checks it. In order:

1. **Heading and role.** The task id and title; the repository by name, the
   checkout path, the work branch and the base ref; "do the task yourself; do
   not redefine, widen or delegate it."
2. **Objective.** From the contract.
3. **This is a correction** (only on a correction version). The correction's
   instructions and the review comments or findings it addresses.
4. **Scope.** Allowed and prohibited paths, whether dependencies may be added
   and CI changed (yes or no), the network mode, "commit on your branch; never
   push", and each of the contract's `constraints.prohibited_actions`.
5. **Read first** (when the contract names any). The contract's `context`
   references and `project_instructions`, each as `kind: ref`.
6. **Acceptance criteria.** Each criterion's id and text.
7. **Checks.** The `required_verification` commands, verbatim, to run and fix
   what fails; an artifact entry is the file to write in the report directory.
8. **Report.** Write `/crucible/report/report.yaml` against
   `report-schema.json` with `schema_version: "1.0"` (the format version, not
   the schema's name, hades #181), `summary`, `acceptance_mapping` (one entry
   per criterion id, which it lists, hades #187), `proposed_pull_request`, and
   the four lists; then run `crucible-report check` and fix every problem it
   prints (hades #215).
9. **If you are stuck.** Write `/crucible/report/blocked.md` saying what
   blocks you and what you tried, then stop; with the contract's
   `escalation.conditions` listed as the cases to stop in.

No exit codes (a model cannot set its harness's exit code; `blocked.md` on a
clean exit is the escalation, 16), no precedence list, no author line (the
collector commits as the policy's author, 08), and no log capture (Crucible
re-runs the checks, 11). Lists and booleans are rendered as words, never as
Python values.

## Harness-specific delivery

The bundle is the same for every harness. How the harness is pointed at it
differs (07): all harnesses use the single generated, untracked `AGENTS.md`
shim when the checkout has no applicable project instruction file. Claude Code
uses the existing project `CLAUDE.md` when one is present, so Crucible writes
no `AGENTS.md` shim in that case. Codex gets `IDENTITY.md` on stdin ahead of
the prompt; AGY gets `--add-dir /crucible/identity` and a short argv prompt
that says to read `IDENTITY.md` first. Hermes gets the text of `IDENTITY.md`
in its prompt, ahead of the pointer (FDY-0140): its launch wrapper reads the
mounted file, so the argv Crucible builds still carries only the pointer.

Shims are written by Crucible after checkout, listed in
`.git/info/exclude`, and their absence from the diff is a gate (11).

## What is never in the bundle

Credentials, the Crucible API token, GitHub tokens, other tasks'
contracts, external review feedback that Foundry has not turned into a
correction contract, the orchestrator's own identity, or any file the
contract did not name.
