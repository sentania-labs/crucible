# Advisory gates (FDY-0138, ADR 0024)

## Decision

The operator, 2026-09-29: "We need to let the review be our enforcement rather
then dictating behavior" and "I want to stop fucking around ... so do what it
takes". Hades had failed correct work from Hermes on the lab because it touched
a file outside a narrow `allowed_paths` list, or because its report lacked a
field. Foundry's contract set the split: hard gates only where the damage is
real or a claim is false, everything else information for the reviewer.

## What was built

- `gates.advisory` in the policy, absent meaning the default set
  (`scope_contained`, `report_present`, `criteria_mapped`,
  `run_evidence_present`). No policy version is rewritten; the lab's
  default-software v8 and hades-self-hosting v1 take the default when read.
- A prohibited path stops the task even though `scope_contained` is advisory,
  and no report at all stops it even though `report_present` is advisory.
- `no_secrets` can never be made advisory, like the review itself.
- `gate_results.blocking` and `gate_results.findings` (migration 0028) record
  each evaluation's class and its advisory findings.
- `verification_ran` names a worker's report that says a check passed when
  Crucible's re-run failed it. The worker's own exits are kept on the claim's
  evidence row as `claimed_checks` (id and integer exit only).
- The task view, the gate list, the admin UI's Tasks page and the wakes list
  what is "for the reviewer". The Routing page, `/v1/admin/gates/advisory` and
  `crucible admin gates` show and edit the advisory set.
- The fake provider gained `prohibited-path`, which the tests that need a
  blocking scope failure now use; `out-of-scope` now reaches the review.

## Choices made here

- Making a gate outside the default set advisory is an operator-only setting,
  recorded as a decision, like `allow_no_ci`. The contract left the choice to
  the policy; this keeps the choice with the operator.
- The local admin CLI has no principal row, so an operator-only setting it
  saves is recorded on the `policy_uploaded` event and not as a Decision row.
  Before this change no local CLI verb could save one.
- No new event kind: a save is a policy upload and is audited as one.
- After the non-author review round (2026-09-29): no report at all blocks (the
  contract scoped the advisory part to a missing or malformed judgement
  field); `no_secrets` is fixed blocking, because a pushed secret cannot be
  taken back and never committing a secret outranks the contract's "the
  policy decides"; and the wake summary names gates only, keeping paths the
  worker chose in the structured list.
- After Codex's review of PR 231 (2026-09-29): a `report.yaml` that is not
  YAML, or not a mapping, was recorded as no report and so blocked. It is now
  a present report that did not parse, with the parser's problem and line and
  column as its parse error, so it goes to the reviewer. The message is built
  from the parser's problem and position, not its excerpt of the file, and the
  unparsed text is secret-scanned under `no_secrets` like a parsed report. The
  fake provider gained `malformed-report` for the test.
