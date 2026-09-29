# The first real pull request does not get stuck (FDY-0139)

Seven ways the delivery half stalled with no move left to anyone, fixed together on
the operator's go of 2026-09-29 ("You have approval for those") under the operator's
direction of the same day: review is the enforcement, Crucible does anything
mechanical, and hard failures belong only where the damage is real or a claim is
false. What each fix does is in spec 23, spec 09 and spec 17, and why in ADR 0025;
this note records the choices made along the way.

## Decisions

- **Settled, not deleted.** A `fix` disposition used to hold the task for ever, because
  dispositions are add-only and the gate counted every comment on every head. The fix
  does not delete or supersede a disposition: a comment whose reviewed head is one
  Crucible pushed and a later accepted head replaced, and which was made before that
  head appeared, simply leaves the count. The reviewed head is the comment's
  `original_commit_id` where GitHub gives one, since `commit_id` follows the pull
  request forward on lines a later head left alone. The time cutoff came out of the
  review round: a reply on an old thread keeps its thread's original commit, and
  without the cutoff it would have been settled unread.
- **Merged or closed from anywhere, including `head_diverged`.** A person merged or
  closed the pull request; that is a fact, and the task records it. A task already
  stranded behind a pull request recorded merged or closed is moved on the next tick,
  because such a pull request is never polled again.
- **The waivers are operator-only Decisions, not new endpoints.** They reuse `POST
  /v1/tasks/{id}/decisions` and its audit trail (a `decisions` row and a
  `decision_recorded` event), so they needed no migration and no new event kind. The
  contract says "operator decision", so an orchestrator token is refused, the same
  rule as `release_authorization`. The UI's button is on a new task page
  (`/ui/tasks/{id}`), since there was no task page before; the Tasks page now lists
  every task with a pull request in delivery and links to it. The waiver reason is the
  decision's verbatim record and is required.
- **`accept_no_ci` means nothing ran.** It turns certification to `skipped` only when
  no check run or workflow run exists on the accepted head, a run a path filter
  skipped counting as none. A repository that does have CI keeps certifying it; the
  waiver is not a way past a red or a pending check.
- **A re-run decision names its runs.** GitHub re-runs a job as a new check run but
  re-runs a workflow under the same run id, so a run is identified by its id and when
  it concluded, and the decision's `ci_decision_recorded` event lists the failed runs
  it is about. Those are stale; anything else is a fresh result. (A first version
  compared only times; the review round showed a re-run that failed just before the
  decision would then have been hidden.) The API cannot write the fenced
  certification row, so the supervisor marks it stale on its next poll; a CI decision
  or a waiver recorded since the last poll makes that poll due at once.
- **No sleep inside the tick.** The transport raises a rate-limit refusal at once with
  GitHub's `retry-after`; the delivery coordinator stops polling and publishing until
  that time passes, measured on the supervisor's clock. A publication interrupted by
  the rate limit stays in `publishing` and resumes, rather than landing in
  `publish_failed` and needing a decision for something that was only a wait, and a
  quota checkpoint push stays pending the same way. Outside delivery the transport no
  longer waits either: a checkout-token mint that meets the rate limit fails the
  launch as an environment failure whose message names the rate limit, and an admin
  call reports it at once. Deferring a launch that has already started preparing is a
  change to the supervisor's launch path and is left as a follow-up.
- **The log excerpt is the tail of the failed job.** Followed through GitHub's redirect
  without the token, fetched once per failed run, and kept across polls while that run
  is still the failure.
- **No new tunable.** Nothing here adds a setting, so nothing needed a UI control
  beyond the two waiver buttons.
