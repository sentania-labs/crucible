# 23. GitHub delivery: publication, PR observation, external review, CI certification

Crucible, not the worker and not Foundry, performs every routine GitHub
mutation and watches the PR afterward. Workers edit, run checks, commit
locally, and write a claim. Foundry interprets and decides. This file is the
delivery half of the task lifecycle (09).

## Authority model

- Crucible holds a GitHub App private key (the credential it owns, 12, ADR
  0017) and mints
  short-lived installation tokens scoped to one repository, on demand, for
  one publisher job at a time. Tokens live in memory and in the tmpfs of
  the publisher container, never elsewhere.
- App permissions: Metadata read, Contents read/write, Pull requests
  read/write, Checks read, Actions read, Issues read (for
  `GET /issues/{n}/reactions` only, S12 rerun). Nothing else, and no
  Issues write. Repository
  registration (`PUT /repositories/{name}`) records the installation ID
  the App has for that repository; the key never appears in the record.
  The GitHub page's repository picker fills it (25): it lists what each
  installation covers, grouped by account, using a token scoped to
  `metadata: read` that is discarded before the listing returns, and a pick
  registers the repository with that installation ID and the default branch
  GitHub reports.
- **The push remote is derived from the repository's `owner/name`, not from
  its registered url.** The registered url is the fetch source: it is what
  the preparer and the collector clone, and those containers hold no GitHub
  credential. Keeping the two separate lets a deployment point the fetch at
  a local mirror of a private repository, which a worker could not clone
  otherwise, while the publisher still pushes to GitHub with the App token.
  In an ordinary deployment the two resolve to the same GitHub repository.
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
   collector (08), uid 1000, on **its own egress network** with an
   allowlist of `github.com` and `api.github.com` only. That allowlist is
   narrower than the workers' proxy permits, which is the point: the one
   container holding a GitHub credential must not sit on a network that
   reaches every model endpoint a harness needs, so the publisher gets its
   own network, its own proxy, and its own allowlist, generated beside the
   workers' one. Mounts: an empty working directory (rw, tmpfs), the branch
   bundle artifact alone (ro), an output dir (rw). The bundle is copied out
   of the collector's output into a directory of its own first, so the
   container holding a credential sees one file and not the diff, the
   report copy, and the collected tree beside it.

   **The token is handed over on stdin, never copied in.** The container is
   created with stdin open, started, and attached to through the Docker API
   (`POST /containers/{id}/attach`, which is `docker run -i` by another
   name); the value is written to that stream and the write side is closed.
   The socket proxy must permit that endpoint. `docker cp` is not an
   option: it cannot reach a tmpfs inside a `--read-only` container, and
   without `--read-only` the value lands on the writable layer, which is
   disk (S10). The token is never in `Env`, in `Cmd`, in a bind source, or
   in Crucible's own argv. Inside the container it is a file on tmpfs read
   by a git credential helper that answers only for the configured protocol
   and host.
4. Inside, the repository is **built from the bundle**, not cloned from a
   cache: `git init`, fetch `base_ref` from the remote the publisher is
   about to push to, `git bundle verify`, then fetch `work_branch` out of
   the bundle. The bundle is `base_ref..work_branch`, so it names
   prerequisite commits and neither verify nor fetch will look at it until
   the repository holds them; the base fetch is what supplies them. Nothing
   else enters this container: not the worker's tree, not the worker's
   `.git`, not a cache a worker could have influenced. Then verify the
   fetched head equals the collected head SHA Crucible recorded; verify
   every commit's author and trailer match policy; push `work_branch` to
   the derived push remote without force. **The commit-policy check aborts
   the publication**: an author or trailer that does not match policy
   refuses with its own exit code before the push is attempted, the token
   goes first, and the problems reach the event, the wake, and
   `publish_failed`. A remote head that is not an ancestor of the bundle
   head (someone
   pushed out of band) fails the push, records `publish_failed` with the
   remote head, and wakes Foundry; Crucible never force-pushes.
5. From Crucible (API calls, same token): `ls-remote` to confirm
   `branch_pushed_at_head`; then open the PR if none exists for this task,
   or reuse the existing PR whose head just changed. **A reused PR is
   reconciled against the contract, not merely assumed to carry the new
   head**: its base is sent on the update, and a PR whose base or draft
   flag does not match what this publication expects fails publication with
   the mismatch recorded rather than advancing. GitHub does not accept
   `draft` on that endpoint, so a reused PR that is a draft is reported and
   refused, never silently corrected. PR title is the
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
   Nothing worker-asserted appears in the body as a verified fact, and
   nothing worker-asserted acts. Worker text is **defanged** before it is
   rendered: every closing keyword that precedes a reference is escaped, so
   a fix-and-reference line inside a limitation cannot close an issue
   nobody authorized, and every at-mention is escaped for the same reason,
   because a mention notifies a person, subscribes a team, and, for a
   provider whose reviewer answers its own name, triggers a review under
   whatever identity opened the PR. Escaping leaves the sentence readable
   and stops the provider acting on it. Backticks are not protection: the
   provider reads the raw body, not the rendered HTML. A proposed title
   carrying a secret pattern, a closing keyword, or an at-mention is
   refused rather than rewritten, because a silently rewritten title is a
   claim Crucible did not make; the secret scan runs before the length
   check, so a long title containing a token is refused for the reason that
   matters.
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
  Handling of each delivery: read the raw body under a **1 MiB cap**,
  enforced both on the declared `Content-Length` and on the read itself,
  because the HMAC is computed over the raw body and the body therefore has
  to be read before it can be verified; verify the HMAC against that body
  in memory; reject and count on mismatch, storing nothing. **A rejected
  delivery stores nothing an unauthenticated caller chose**, its claimed
  delivery id included: an unsigned request is an unauthenticated claim
  about everything in it. The rejection is counted as an event carrying the
  reason and, for the claimed event, one name from the handled set above or
  `unrecognized`. On a verified delivery: parse; extract
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
  cannot move the task. Foundry decides `recollect` (the task re-enters
  `scheduled` with a `correct` execution against the remote `work_branch`,
  which is where the divergent head is, so the new head produces a
  completion claim and a bundle of its own; then the full pre-PR path,
  internal review as the policy and Foundry decide, acceptance, and
  `publishing`, which verifies the remote already matches) or `reject`.
  Re-entering at `reported` would put the new head in front of gates with
  no claim behind it (09).

Foundry is not required to remain connected for any of this.

## Triggering the external reviewer

S12's rerun (docs/spikes/S12.md, 2026-09-16) settled it: with the
repository's Codex setting "review all pull requests" enabled, an
App-authored PR is reviewed automatically (pickup 11 s after open,
completion 101 s) with no human comment. **Repository onboarding
prerequisite**: the Codex GitHub App must be installed on the repository
with review of all pull requests enabled (code and security review as the
operator chooses). GitHub exposes neither setting, so registration
(`PUT /repositories/{name}`) requires an operator **attestation**
(`external_review.attested_all_prs: true`, with the attesting principal
and time recorded as an event) whenever the repository's policy requires
external review; a registration without it is accepted only with
`external_review.required_rounds: 0`. Optionally `POST
/admin/repositories/{name}/probe-review` opens a throwaway App-authored
PR, waits for a reviewer signal, closes it, and records the observed
result; the first real task's reviewer signal also updates the record,
and a repository whose PRs get none is reported by the admin status. The
provider's trigger comment, posted by the orchestrator under the
operator's account, remains the fallback for a repository where automatic
review is not enabled and for an explicit re-review after a correction,
both recorded as events. Crucible never posts it: the client's
`post_issue_comment` exists for a repository that configures something
else and refuses unless `github.allow_issue_comments` is on, and a
retrigger opens a new cycle and wakes the orchestrator with reason
`external_review_trigger_needed` instead.

**Crucible never authors the trigger phrase, in any text it writes.** The
provider's trigger is an at-mention of its own name, and the provider acts
on the raw text: a PR body, a commit message, or a comment Crucible
authored that contained it would perform the trigger under the App's
identity rather than the operator's, which is exactly the act 23 reserves
for the orchestrator. So the phrase in its at-sign form never appears in a
body Crucible renders, a commit message it writes, or a comment it posts,
and the renderer defangs every at-mention in worker-asserted text for the
same reason (a limitation or risk quoting the phrase would otherwise
trigger a review). Quoting it in backticks is not protection; the provider
reads the body, not the rendered HTML. The phrase is described, never
written, in this specification for the same reason.

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
only. The provider's summary comment
is posted within about ten seconds of the PR opening, minutes before any
verdict, and is then edited in place when the review lands; it never counts
as a round. It is recognized by the marker the provider puts at the top of
it, and a comment carrying that marker is not a round even under a policy
that has deliberately added `comment` to `accepted_signals`. A comment is
not an accepted signal by default at all (05b). The review object, its
comments, and the provider's no-findings result do count. Installation tokens are
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
signal to the open cycle by head SHA and, where the signal names one,
component. GitHub does not label the reviewer's signals by component, so
the rule is: a review object or review comment attaches to the component
its body names if any, else to `code`; a reaction-only clean result
(`+1` with no review object) completes **every** configured component of
the cycle at once, because the provider emits one combined verdict. The
`external_review_rounds` gate counts completed cycles, never individual
signals; a cycle with two components and one review object with findings
stays open until the second component's result or the cycle timeout. When `retrigger_after_correction` is true the same
trigger path applies to every corrected head: Crucible wakes the
orchestrator with reason `external_review_trigger_needed`, the
orchestrator posts the trigger under the operator's account, and a new
cycle opens on that head.

## External review (bounded input, not a loop)

- A round is one completed cycle. **A signal never opens a cycle**: the
  cycle row is created at publication of a head, or when a retrigger is
  recorded, with the components the policy expects. Signals complete the
  components of a cycle that is already open. A signal counts only from a
  login in `external_review.reviewer_logins` and only when its kind is in
  `external_review.accepted_signals`: by default a submitted review or a
  `+1` reaction on the PR (the configured reviewer's "no findings"
  signal), with a comment accepted only where a repository opts in (05b).
  Any other user's activity, including reactions, is recorded but satisfies
  nothing. Rounds are counted per PR across heads.
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
- Advancement out of `external_feedback_received` is the dispositions
  gate's decision: every received comment dispositioned **and none of them
  `fix`**. A `fix` is Foundry saying the work is not done, so a
  dispositioned-but-fix set is not advancement, it is a correction. Only
  once that gate passes does the round count choose the branch: at or above
  `required_rounds` the task goes to `awaiting_ci_certification`, and with
  rounds outstanding it returns to `awaiting_external_review` to wait for
  the next cycle. Conditioning the advance on both gates at once would make
  the second branch unreachable and park a task with `required_rounds: 2`
  forever. With the
  default policy (one round, no retrigger after correction, no
  requirement on the final SHA) a correction never causes a second round.
  Other repositories may set `required_rounds`,
  `retrigger_after_correction` (on each corrected head Crucible opens a new
  cycle and wakes the orchestrator with reason
  `external_review_trigger_needed`; the orchestrator posts the trigger
  under the operator's account, because Crucible never posts it), and
  `require_review_on_final_sha` (the last accepted signal must name the
  accepted head) differently.
- Nothing received is overdue silently: `wait_timeout_hours` produces a
  repeat wake. **The clock starts when the task entered the state it is
  waiting in**, read from that transition's own event, not when the PR was
  opened. The same rule governs `ci_certification_overdue`. Measuring from
  the PR would make a correction on a three-day-old PR overdue on its first
  poll.

## CI certification

- Required-check set: the policy's `ci_certification.required_checks`,
  or, when empty, every check the repository's branch protection or
  ruleset marks required for the base branch, or, when that is empty too,
  every non-skipped check run and workflow run observed on the accepted
  head SHA.
- Green: the set is non-empty and every member concluded `success` on the
  accepted head. `success` and nothing else: GitHub uses `neutral` for a
  check that ran and declined to judge and `skipped` for one a condition
  excluded, and neither certifies. A required check with either conclusion
  leaves the set **pending** until `wait_timeout_hours` wakes Foundry,
  which is what the timeout is for. A repository that excludes a required
  check on unrelated paths narrows `ci_certification.required_checks` or
  sets `allow_no_ci`; it does not get a pass from the conclusion. **An empty set is `pending`, never green**: before GitHub
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
with the closer recorded. The closer is not on the PR itself: `GET
/pulls/{n}` carries `merged_by` and no closer, so Crucible reads the actor
from the issue events timeline (`GET /issues/{n}/events`, the last `closed`
entry). That second call is **best effort**: a repository whose timeline
the App cannot read records the close with no actor rather than failing the
observation. Crucible has no merge endpoint.

## Ready-for-merge report

When the task reaches `ready_for_merge`, the wake carries: PR URL, final
head SHA, external review summary with every disposition, CI certification
summary with check names and run URLs, the internal review reference, and
the correction history. Foundry reports the PR as ready to the operator
from this record; Crucible produces the facts, Foundry the sentence.
