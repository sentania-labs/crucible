# The commit trailer is not required; the task record is the paper trail (FDY-0143)

## Decision

The operator, 2026-09-29, after Foundry recommended that the `Crucible-Attempt` trailer
stop being a requirement and the task record be the paper trail: "So then we pull back
that message/required comment?" FDY-0135 (PR 228) had added the commit-msg hook, a
publisher refusal, and the `commit_policy` gate failing a commit without the trailer or
with another author. This change keeps the hook and removes every requirement.

## What changed

- The publisher script no longer checks commits. It had refused a push (exit 6,
  `commit-policy` step) for an author or trailer that did not match policy; that step,
  its exit code, and the `author_problems` and `trailer_problems` fields of the
  publish outcome and the `publisher_finished` and `task_publish_failed` events are gone.
  What it still guarantees: the bundle matches its seal (exit 7), the fetched head is the
  reviewed and accepted head (exit 4), and the push is never forced.
- The collector checks authors only. `commit-policy/trailer-problems.txt` is no longer
  written, and `CommitPolicyCheck` has no `trailer_problems`.
- The `commit_policy` gate always passes once the collector has run. Commits authored by
  someone other than the policy's `author_email` are named in its detail, starting "for
  the reviewer:". A collector that could not read the commits passes too, and says so.
  The gate is still evaluated whatever the policy lists. PR 231's advisory gate mechanism
  was not on main when this was written; once it is, `commit_policy` is a natural
  advisory gate.
- The commit-msg hook still adds `<commit_trailer>: <external_id>` as a courtesy. Nothing
  reads it. `git.commit_trailer` stays in the policy schema for the hook.
- IDENTITY.md section 10 is one line: "Commit your work on `<branch>`."
- The task view (`GET /v1/tasks/{id}`) carries `delivery`: the work branch, the head
  Crucible pushed and when (from the latest `branch_pushed` event, falling back to the
  pull request's branch and head), the PR number, link and state, and once merged the
  merge commit SHA, who merged, and when. The PR summary in the same view also carries
  `work_branch`.
- The admin UI has a task page, `/ui/tasks/{id}`, showing the same record, in local time.
  The Tasks page links each task that needs attention to it, and lists the tasks updated
  in the last 14 days, newest first, with links.

## Tests

- `tests/unit/test_commit_trailer.py::test_a_bundle_without_the_trailer_by_another_author_publishes`
  runs the real publisher script against a local bare repository with a bundle whose
  commit has no trailer and another author, and sees it pushed.
- `tests/integration/test_github_delivery.py::test_a_commit_by_another_author_without_the_trailer_publishes`
  takes such a worker through review, acceptance, publication, CI and merge against the
  fake GitHub, and reads the paper trail from the API and the UI task page.
