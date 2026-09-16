# 23. GitHub delivery: publication, PR observation, external review, CI certification

Crucible, not the worker and not Foundry, performs every routine GitHub
mutation and watches the PR afterward. Workers edit, run checks, commit
locally, and write a claim. Foundry interprets and decides. This file is the
delivery half of the task lifecycle (09).

## Authority model

- Crucible holds a GitHub App private key (mounted, 12) and mints
  short-lived installation tokens scoped to one repository, on demand, for
  one publisher job at a time. Tokens live in memory and in the tmpfs of
  the publisher container, never elsewhere.
- App permissions: Metadata read, Contents read/write, Pull requests
  read/write, Checks read, Actions read. Nothing else. Repository
  registration (`PUT /repositories/{name}`) records the installation ID
  the App has for that repository; the key never appears in the record.
- Workers receive no GitHub credential. The worker's checkout has its
  `origin` URL replaced with a placeholder; a `git push` inside a worker
  fails with no credential and is a recorded prohibition, not a gate.
- Any future mode that gives a worker direct GitHub access is a policy
  exception with its own ADR.

## Publication (task state `publishing`)

Runs only after: pre-PR gates passed for the collected head, the internal
review is recorded, and Foundry's `AcceptanceResult` for that head is
`accepted` (policy `publish_requires_acceptance`).

1. Event `publish_started` with the head SHA and the bundle artifact ID.
2. Mint an installation token for the repository (expires in one hour; the
   job is bounded well below that).
3. Launch a **publisher container**: the same hardened shape as the
   collector (08), `--network` on the egress network with an allowlist of
   `github.com` and `api.github.com` only, uid 1000, mounts: a fresh clone
   from Crucible's bare cache (rw, tmpfs), the branch bundle artifact (ro),
   the token as a file on a tmpfs mount read by a git credential helper,
   an output dir (rw). It never sees the worker's tree or the worker's
   `.git` directory; the bundle is the only carrier of the worker's
   commits, and `git bundle verify` runs before fetch.
4. Inside: fetch `work_branch` from the bundle; verify the fetched head
   equals the collected head SHA Crucible recorded; verify every commit's
   author and trailer match policy; push to `origin work_branch` without
   force. A remote head that is not an ancestor of the bundle head (someone
   pushed out of band) fails the push, records `publish_failed` with the
   remote head, and wakes Foundry; Crucible never force-pushes.
5. From Crucible (API calls, same token): `ls-remote` to confirm
   `branch_pushed_at_head`; then open the PR if none exists for this task,
   or leave the existing PR whose head just changed. PR title is the
   claim's proposed title after validation (length, no secret patterns, no
   closing keywords). PR body is rendered by Crucible from the contract
   and **verified** evidence only:
   - objective and acceptance criteria with their verified mapping,
   - each required verification command with the verifier's exit code and
     log artifact ID (never the worker's own logs as proof),
   - the internal review reference (reviewer kind, report artifact ID),
   - the correction history if any,
   - the attempt ID, image digest, harness and version,
   - the authorized closing references from `deliverables[].closes` as
     `Closes <ref>` lines; no other closing keyword survives rendering,
   - limitations and risks from the claim, labeled as worker-asserted.
   Nothing worker-asserted appears in the body as a verified fact.
6. Event `publish_completed` with PR number, URL, head SHA, body hash.
   Then `pr_exists_head_matches` evaluates and the task moves to
   `awaiting_external_review` or `awaiting_ci_certification`.
7. Publisher container removed; token discarded. Any failure between 2
   and 6 is `publish_failed`, with the step and the API response class
   (never the token) recorded.

## Observation

After publication Crucible watches the PR until the task is terminal.
Polling is the complete observation path; webhooks only shorten latency.

- **Polling** (always on): every `github.poll_interval_seconds` (default
  120) the supervisor fetches, for every PR in an observed state: the PR
  (state, head SHA, mergeability, merged flag and merger), its reviews,
  its review comments, its issue comments, the reactions on the PR
  itself, on each review, and on each comment, the check runs and check
  suites for the current head, the workflow runs for the current head,
  and the base branch's required checks. Reactions are polled because
  GitHub delivers no webhook event for them; the configured reviewer
  signals "reviewed, no findings" with a thumbs-up, so the reaction poll
  is part of every cycle, not an extra.
- **Webhooks** (optional accelerator, off by default on a workstation;
  Q13): `POST /v1/github/webhook` accepts `pull_request`,
  `pull_request_review`, `pull_request_review_comment`, `issue_comment`,
  `check_run`, `check_suite`, `workflow_run`, and `push`. A review or
  comment delivery triggers an immediate reaction poll for its subject.
  Handling of each delivery: verify the HMAC against the raw body in
  memory; reject and count on mismatch, storing nothing; parse; extract
  only the fields Crucible uses; run every user-controlled text field
  (review bodies, comment bodies, titles) through the secret scanner and
  redaction; store the delivery ID, event and action, normalized fields,
  and a SHA-256 of the original body; discard the raw body. The same
  normalization and scanning applies to text fetched by polling before it
  is written to `review_comments` or `external_reviews`.
- Every observed change is an event: head changed (and by whom), review
  received, comment received, reaction received, check concluded, PR
  closed or merged.
- **Head changed out of band**: a head Crucible did not push moves the task
  to `head_diverged` (09). The previous head's acceptance, review report,
  and gate results are marked superseded and kept as history. Nothing
  about the new SHA is trusted: CI on it is observed and recorded but
  cannot move the task. Foundry decides `recollect` (Crucible fetches the
  new head into a fresh collector, produces a bundle, and the task
  re-enters `reported` for the full pre-PR path, internal review as the
  policy and Foundry decide, acceptance, and `publishing`, which verifies
  the remote already matches) or `reject`.

Foundry is not required to remain connected for any of this.

## Triggering the external reviewer

S12's rerun (docs/spikes/S12.md, 2026-09-16) settled it: with the
repository's Codex setting "review all pull requests" enabled, an
App-authored PR is reviewed automatically (pickup 11 s after open,
completion 101 s) with no human comment. **Repository onboarding
prerequisite**: the Codex GitHub App must be installed on the repository
with review of all pull requests enabled (code and security review as the
operator chooses); Crucible's registration check records whether the
first App-authored PR received a reviewer signal and reports a
repository whose PRs get none. The orchestrator-posted `@codex review`
comment under the operator's account remains the fallback for a
repository where automatic review is not enabled and for an explicit
re-review after a correction, both recorded as events.

What the reviewer emits, as observed: a clean result is **reactions on
the PR only**: `eyes` on pickup (deleted on completion, so it lives about
90 to 150 seconds) and a durable `+1`; no comment, no review object, no
check run. A result with findings is a review object with inline
comments plus the summary comment. Reactions carry no commit id, so a
reaction's head binding is inferred from the PR head at its `created_at`
against the head history, and `require_review_on_final_sha` cannot be
satisfied by a reaction-only result; a policy that needs a per-head
result must use the trigger comment after each head. Code review and
security review are not separable in the GitHub record and are treated as
one round. A new head does not re-trigger the reviewer by itself.

Reading reactions needs the App permission **Issues: read** (the
endpoint is `GET /issues/{n}/reactions`; comments are readable with Pull
requests read alone). It is added to the App's permission set for that
one call (ADR 0007). Because the pickup reaction is transient, the
supervisor polls reactions every `github.reactions_poll_interval_seconds`
(default 60) while a PR is `awaiting_external_review`, and treats the
durable `+1` as the completion signal; a missed `eyes` is informational
only. The bot's summary comment
is edited in place and never counts as a round; the review object, its
comments, or the bot's no-findings comment do. Installation tokens are
about 390 characters with dots, not the short `ghs_` form, and the
redaction patterns cover both.

A round means one configured review cycle after publication. When the
repository's Codex configuration runs code review and security review in
that cycle, both are components of the same round; the round completes
only when every configured component has completed or reached a terminal
result. Crucible persists a review cycle row per PR head it published
(or per trigger it recorded) with the components the policy expects
(`external_review.components`, default `["code"]`; `["code",
"security"]` where the repository runs both), and attaches each received
signal to the open cycle by head SHA and component; the
`external_review_rounds` gate counts completed cycles, never individual
signals, so one component can satisfy neither a one-round nor a two-round
policy by itself. When `retrigger_after_correction` is true the same
trigger path applies to every corrected head: Crucible wakes the
orchestrator with reason `external_review_trigger_needed`, the
orchestrator posts the trigger under the operator's account, and a new
cycle opens on that head.

## External review (bounded input, not a loop)

- A round is one accepted signal from a login in
  `external_review.reviewer_logins`: a submitted review, a comment, or a
  `+1` reaction on the PR (the configured reviewer's "no findings"
  signal). Any other user's activity, including reactions, is recorded but
  satisfies nothing. Rounds are counted per PR across heads.
- A round with no findings (a `+1` reaction, or a review with no comments)
  moves the task through `external_feedback_received` with nothing to
  disposition; Crucible records it and, when `required_rounds` is
  satisfied, advances to CI certification without a wake for judgment.
- Received: Crucible stores the review, its comments (each with ID, path,
  line, body, reviewed SHA), and reactions, then moves the task to
  `external_feedback_received` and wakes Foundry.
- Foundry reads the feedback and records a `ReviewDisposition` per
  comment: `fix`, `decline`, `out_of_scope`, `already_addressed`,
  `question`, with reasoning. Crucible never forwards feedback to a worker.
- If any disposition is `fix`, Foundry attaches a correction contract (05)
  and the task re-enters supervision against the existing branch. The
  correcting worker reruns every required check; Crucible re-verifies,
  Foundry accepts, Crucible pushes the corrected head.
- Advancement out of `external_feedback_received` requires the
  `external_review_rounds` gate: every received comment dispositioned and
  the completed-cycle count at or above `required_rounds`. With rounds
  outstanding the task returns to `awaiting_external_review`. With the
  default policy (one round, no retrigger after correction, no
  requirement on the final SHA) a correction never causes a second round.
  Other repositories may set `required_rounds`,
  `retrigger_after_correction` (Crucible posts the provider's trigger
  comment after each corrected head), and `require_review_on_final_sha`
  (the last accepted signal must name the accepted head) differently.
- Nothing received is overdue silently: `wait_timeout_hours` produces a
  repeat wake.

## CI certification

- Required-check set: the policy's `ci_certification.required_checks`,
  or, when empty, every check the repository's branch protection or
  ruleset marks required for the base branch, or, when that is empty too,
  every non-skipped check run and workflow run observed on the accepted
  head SHA.
- Green: the set is non-empty and every member concluded `success` on the
  accepted head. **An empty set is `pending`, never green**: before GitHub
  has created any run, or on a repository with no CI, the task waits and
  `wait_timeout_hours` wakes Foundry with `ci_certification_overdue`. A
  repository that intentionally has no CI needs
  `ci_certification.allow_no_ci: true`, an operator-recorded policy
  decision, which makes the gate `skipped` rather than passed. On green
  the task moves to `ready_for_merge` and wakes Foundry.
- Failed: any required check concluded `failure`, `cancelled`,
  `timed_out`, or `action_required`. Crucible captures the check name,
  workflow, job, head SHA, and the available log excerpt (through the
  Actions read permission), writes a `CICertification` row with state
  `failed`, moves the task to `ci_certification_failed`, and wakes Foundry.
  No automatic retry. No automatic worker correction.
- Foundry's `POST /tasks/{id}/ci-decision` records the cause from the enum
  `false_pre_pr_evidence`, `wrong_sha_checked`, `correction_without_checks`,
  `environment_drift`, `flaky_test`, `crucible_verification_defect`,
  `ci_infrastructure`, `other`, and the action: `rerun` (Crucible records the
  intent and wakes the operator to re-run it on GitHub, because re-running
  needs Actions write, which the App does not hold; 22), `correct` (a
  correction follows), `reject`, `cancel`.
- A head that changes while awaiting certification is a divergence
  (above), not a new certification: the task leaves the certification
  path until Foundry decides.
- `wait_timeout_hours` without a conclusion produces a wake with reason
  `ci_certification_overdue`.

## Merge

Merging is the operator's act on GitHub. Crucible observes
`pull_request.closed` with `merged: true`, records the merge SHA, the
merger login, and time, moves the task to `merged`, and wakes Foundry
(informational). A PR closed without merge moves the task to `rejected`
with the closer recorded. Crucible has no merge endpoint.

## Ready-for-merge report

When the task reaches `ready_for_merge`, the wake carries: PR URL, final
head SHA, external review summary with every disposition, CI certification
summary with check names and run URLs, the internal review reference, and
the correction history. Foundry reports the PR as ready to the operator
from this record; Crucible produces the facts, Foundry the sentence.
