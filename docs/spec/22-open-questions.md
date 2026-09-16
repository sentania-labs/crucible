# 22. Decisions taken and questions still open

## Decided by the operator, 2026-09-16 (incorporated in v0.3)

| # | Topic | Decision | Where |
|---|---|---|---|
| 1 | Worker images | Build locally in early phases; publish versioned, digest-pinned images to `ghcr.io/sentania-labs/` once the live harness phase and release workflow exist | 13, 20 |
| 2 | GitHub authentication | GitHub App with Metadata read, Contents rw, Pull requests rw, Checks and Actions read; short-lived repository-scoped installation tokens minted on demand; key and tokens never stored; workers hold no GitHub credential; Crucible owns all routine GitHub mutations | 12, 23, ADR 0007 |
| 3 | Internal review | Required, non-author, before any PR; Foundry decides when and what; Crucible runs it as a `review` execution when asked, Foundry uploads the report otherwise | 09, 11 |
| 4 | External review | Configurable per repository policy; default one Codex round, no retrigger after correction, not required on the final SHA, disposition required; allowlisted reviewer logins only | 05b, 23, ADR 0008 |
| 5 | PR and CI observation | Crucible watches the PR by webhook plus polling; Foundry keeps no loop; feedback never reaches a worker without a Foundry decision | 23 |
| 6 | CI certification | Pre-PR verification is the proof, PR CI the certification; a required failure is an escalation with evidence and no automatic retry or correction; distinct states added | 09, 23, ADR 0009 |
| 7 | Release | Foundry proposes, operator authorizes, Crucible verifies gates and tags; domain designed now, implemented after readiness | 24, ADR 0010 |
| 8 | Timezone | Stored UTC with offset; rendered in the operator's private configuration; repository default UTC | 01, examples |
| 9 | Concurrency | 3 per provider, 1 per subscription harness while auth state is shared | 05b |
| 10 | Retention | Logs and transcripts 90 days, bootstrap archive 180, completed workspaces 14, credential volumes immediate, everything else indefinite; cleanup as events | 05b, 16 |
| 11 | Host-process provider | Designed, not implemented unless a spike proves a harness cannot run in a container; weaker isolation requiring explicit policy authorization | 08 |
| 12 | Crucible delivery | Branches and PRs, tagged releases, tag-triggered SDLC pipeline, GitHub-hosted runners | 20 |
| 13 | Docker authority | Rootless daemon dedicated to Crucible preferred, spike early; host socket as documented temporary fallback; no container other than Crucible ever sees the socket or proxy | 13, ADR 0004 |
| 14 | Prior-art reuse | No code reuse from Sandcastle, earlier Foundry, Stewart, Vault by default; any helper needs an ADR with source, license, justification, attribution, fit | 02 |
| 15 | Harness credentials | Per-harness mounts, narrow disposable copies, allowlisted sync-back, concurrency 1; a worker may misuse its own credential, mitigations listed, broker later | 12, ADR 0007 |
| 16 | Harness versions | No self-update; version pinned per image; digest recorded per attempt; adapter version ranges with refusal; Renovate weekly PRs, canary, explicit promotion, one retained prior version | 07, 13, ADR 0011 |

## Interpretations made while incorporating (confirm or correct)

- **I1. Foundry's acceptance sits between pre-PR gates and the push.** The
  operator's flow says Crucible pushes after the pre-PR gates pass. This
  specification inserts Foundry's `AcceptanceResult` for the collected head
  before `publishing` (policy `publish_requires_acceptance: true`), so the
  outward-facing push under the App identity always follows a recorded
  judgment. It costs one API call per head. Set the flag false to push on
  gates alone.
- **I2. Corrections pass through the full pre-PR path again**, including
  the internal review gate. The policy could exempt corrections from a
  second internal review; the default here does not, because a correction
  is new code under the same author.
- **I3. A PR closed without merge rejects the task**; cancelling a task
  never closes its PR. Both are recorded, neither is reversible by Crucible.
- **I4. Merge is observed, never performed.** Crucible has no merge
  endpoint, matching "I review and merge".
- **I5. `required_checks` resolution order**: policy list, then the base
  branch's protection or ruleset, then every non-skipped check on the SHA.

## Still open (operator judgment required)

- **Q13. Webhook route to the workstation.** GitHub must reach
  `/v1/github/webhook` for webhooks to work locally. That needs a public
  route, which is the operator's decision alone (a public DNS record, a
  tunnel, or nothing). Polling alone is complete and is the default until
  told otherwise. Recommendation: polling only until Crucible runs in
  Kubernetes behind existing ingress.
- **Q14. Who records release authorization.** Option A: the operator holds
  an `operator` role token and records the decision directly. Option B:
  Foundry records it with the operator's verbatim words. Default in this
  specification: A required; B allowed only if the policy says so.
  Recommendation: A, because it keeps the one consequential act on a
  credential only the operator holds.
- **Q15. External reviewer login.** The default `reviewer_logins` value is
  the Codex connector's expected bot login; S12 confirms it. No action
  needed unless the operator uses a different reviewer App.
- **Q16. `source.migrated` in the bootstrap export bundle.** The ledger
  tool's worker asked whether the export should carry the migrated flag.
  Recommendation: yes, as informational metadata the import verifies but
  does not act on.

## New conflicts introduced by the decisions

- **Codex review and CI on `crucible/*` branches.** The external reviewer
  and required checks must be configured on each target repository to run
  on PRs from `crucible/*` branches; that is repository setup, not
  Crucible code, and it is a readiness prerequisite per repository.
- **Egress allowlist still contains `github.com`.** Workers keep read
  access for dependencies and context. Without a credential they cannot
  push, but a worker could still read public repositories the contract did
  not name. Accepted for now; tighten per policy if needed.
- **App permissions versus re-run.** Re-requesting a failed workflow run
  needs Actions write, which the decided permission set excludes. This
  specification keeps Actions read; the `rerun` action in `ci-decision`
  therefore records the intent and asks the operator to re-run, unless the
  operator later grants Actions write. Flagged rather than silently widened.
