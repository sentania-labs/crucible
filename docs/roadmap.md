# Hades roadmap

This is the working plan that turns `docs/vision.md` into milestones with exit tests.
The vision is the operator's directive and wins where the two disagree; this document
is the plan Foundry is executing against it, and it changes as decisions land.
Last revised 2026-09-23.

## Where we are

Crucible v0.5.2 is the proven execution MVP: task contracts, isolated per-attempt
workers carrying Claude Code, Codex, AGY and Hermes in one image, durable evidence,
mechanical completion gates, GitHub delivery, and Docker and Kubernetes providers.
On the lab cluster it does not yet complete an attempt: #91 leaves workers with no DNS
or model route, #93 keeps pods from scheduling, and #95 lets the PID gate pass falsely.
#92 means harness credentials cannot be entered from the UI there, and #94 re-chowns
NFS volumes on every mount. The Foundry bootstrap ledger has not been handed over.

## Standing decisions

All of these are the operator's decisions.

| Decision | Date |
|---|---|
| **Hades** is the product and repository name (vision). | 2026-09-23 |
| The "later decision" the vision leaves open on the subsystem's name is taken: it becomes **`execution`**. Its internal identifiers (namespaces, Secrets, `CRUCIBLE_*` settings, images, database, CLI) are renamed after M1, as one planned migration run through Hades, with an alias period and a coordinated redeploy. New components never embed the name `crucible`. | 2026-09-23 |
| Harness login is driven from the UI on every provider, which answers the vision's open question on #92: it is required now. Harness credentials are not hand-sealed as a substitute. | 2026-09-23 |
| M1 proves two paths: Claude Code (subscription) and Hermes to the local model gateway. AGY follows once the Kubernetes login exists. Codex follows #42. | 2026-09-23 |
| Workers never get a Docker socket. Heavy test tiers run in branch CI (#85). | 2026-09-22 |

## Bootstrap

### M0a: rollback point

- #96: service `latest` is copied on the registry from the version tag, never pushed
  from a fresh local build, the rule main already applies to the worker images since
  #90 (after v0.5.2).
- This roadmap and the vision are committed.
- v0.5.3 is tagged as the last Crucible-era release (the operator's go).

**Exit:** the release run is green and both images pull by the digests the release
names.

### M0b: an honest, usable lab deployment

1. #91 with #58: egress allows that work under Cilium with kube-proxy replacement
   (selector or CNI-aware rules, not `ipBlock` on service addresses), and a canary
   that proves DNS resolution and model-endpoint reachability rather than only
   API-server denial. The login Job's allows are narrowed to login endpoints.
2. In parallel: #93 (requests below limits, configurable, with a render-time total of
   requested resources), #95 (the PID gate reads the pod's real limit, or the provider
   sets one), #94 (`fsGroupChangePolicy: OnRootMismatch`), #89 (an empty Hermes Secret
   is no credential).
3. #92: the Kubernetes login Job, driven from `/ui`, and Hermes gateway key entry from
   the UI on Kubernetes. This changes a documented design: spec 26 and
   `docs/deployment.md` deliver harness Secrets through the GitOps repository today.
   A new ADR, with amendments to specs 12, 25 and 26, makes the service the single
   owner of the harness credential Secrets and takes them out of GitOps, because a
   Secret written by both the service (login, token sync-back) and GitOps drifts. The
   database, GitHub App and TLS Secrets stay with GitOps.
4. `docs/deployment.md` matches what ships.

**Exit (seen working):** on a deployment whose GitOps repository carries no harness
credential Secret, the operator logs Claude Code in and enters the Hermes key in the
browser, then one real attempt on Claude Code and one on Hermes each reach a terminal
state with evidence Foundry has read.

### M0c: authority handoff

Register the repositories (spec 25), then import, verify and commit the Foundry
bootstrap ledger on the deployed instance (spec 15). Commit and mark-migrated are irreversible
and wait for the operator's verbatim go. Foundry's start-of-session then reads tasks
and wakes from Hades only.

**Exit:** the bootstrap ledger is frozen and Hades is the system of record.

### M0d: a truthful product

- README and operator documentation describe Hades, with `execution` as the subsystem.
- Open issues are triaged as the vision sets out: bootstrap blocker, execution
  hardening, later. #85 is the one M1 dependency outside the bootstrap blockers.
- The repository is renamed to Hades on the operator's go. GitHub redirects the old
  URL and image names are unchanged, but registrations store the repository URL and
  the push remote is built from `owner/name` (specs 03 and 23). So the rename lands
  before M0c registers repositories, or M0c re-registers after it.

**Exit:** the README a newcomer reads describes what is deployed today, every open
issue carries one triage label, and a clone, a push and a registered-repository
token check work under the repository's final name.

### M1a: Hades can test itself from a pod (#85)

- Declared test services (PostgreSQL first) run as per-attempt sidecars, reachable on
  localhost only. The suite uses the service when it is present and falls back to
  testcontainers when it is not, so one suite runs on a laptop, in CI and in a pod.
- Branch CI already runs every job on every push. What is new is that the service
  observes it: branch CI green becomes a required gate before review and the pull
  request, in the slot local tiers hold today. The heavy tiers stay on GitHub-hosted
  runners, as `ci.yml` places them for a public repository; moving any job to the
  self-hosted pool is a separate decision.
- CI uploads a diagnostics bundle on failure only.

**Exit:** a worker on the lab cluster runs the unit and integration tiers against its
PostgreSQL sidecar, and an attempt whose branch CI is red cannot reach review.

### M1b: a deterministic model for tests

A scripted OpenAI-compatible stub behind a gateway model alias, so tests of the Hermes
path do not depend on a live model's output.

**Exit:** a Hermes-path integration test passes identically on repeated runs with no
live model reachable.

### M1c: acceptance

The operator hands Foundry a bounded change to Hades. Foundry scopes it and submits a
contract; Hades launches the worker on the lab cluster; evidence and gates are
captured; branch CI gates it; Foundry reviews before the pull request opens; the
external review round runs; Foundry accepts, corrects or escalates. Once on Claude
Code, once on Hermes.

**Exit:** the operator can truthfully say "Foundry uses Hades to develop Hades."

## Self-sustaining

From here, Hades work is dispatched only through Hades.

### M1.5: burn-in

- The `execution` rename.
- The hardening backlog as the burn-in queue: #39, #40, #41, #55, #59, #60, #63, #65,
  #66, #76.
- AGY on Kubernetes through the UI login.

**Exit:** the rename and at least five further changes are merged through Hades,
with no fix to Hades made outside it in that period.

## Product milestones

Order after burn-in follows real use.

### M2: persistent principal agent

- A dedicated service owns every conversation. Hades' own append-only log is the
  source of truth; a harness session is a cache that can be rebuilt from it.
- Reasoning runs in the official harness binaries in their long-lived structured
  modes (Claude Code with stream-JSON input and output, `codex app-server` over
  stdio), signed in with the operator's own subscription and a login dedicated to the
  principal so its token refreshes never collide with workers. Hermes serves cheap
  turns.
- One active turn per conversation; reconnect resumes the same conversation from any
  client.
- The principal's authority is the Hades API plus read-only context, not a shell.
  Anything that changes code is an `execution` task.
- Approvals become first-class records: pending, approved, denied, expired; resolved
  once, atomically; kept in a permanent decision log. "Expired" (nobody answered) is
  never shown as "denied" (someone decided).
- First step is a spike: one conversation through each harness, the pod killed mid
  turn, the session volume wiped, and a worker running concurrently on the same
  subscription.

**Exit:** close the laptop, return from another client, continue the same
conversation, and delegate through Hades.

### M3: agent identity and curation

- An agent card: identity, responsibility, tools, skills, routing preference, sample
  requests, and an enabled flag kept separate from observed health.
- Skills state when to use them and when not to, name the sibling skill for the
  adjacent case, and declare the tools they may use. They are injected at dispatch,
  never committed to target repositories.
- "Test the agent": assertions on the tools an agent called and their order against
  mocked tools, routing cases (should route here, near miss, should route nowhere),
  and an optional model-judged pass where a judge outage is an error, not a skip.

**Exit:** the operator creates an agent in the UI, attaches a skill and a tool, runs
its tests there, and enables it.

### M4: work and focus

Lightweight cards that reference GitHub and other sources rather than copying them.
The first view answers "what needs me?" from pending approvals, agents waiting, and
real commitments, with lab and pet-project work visibly separate.

**Exit:** the operator uses that view, not GitHub or chat scrollback, to find what is
waiting on them.

### M5: routines and the daily view

A durable scheduler with leases, a stored next run and an explicit missed-run policy.
Each run gets its own conversation. Each routine declares its pre-approved tools; a
run that needs a new approval parks in "what needs me".

**Exit:** a routine survives a service restart without a lost or doubled run, and the
daily view is the one the operator actually opens.

### M6: tools and transports

Chronicle, Coppermind, GitHub and Discord, each when a real flow needs it. Inbound
triggers carry a per-trigger token stored hashed, a payload schema, and a delivery
log, behind the durable queue.

**Exit:** per tool, the real flow that justified it runs end to end.

## Explicitly deferred

As `docs/vision.md` lists: org charts, multi-company support, a Paperclip clone, a
universal knowledge store, vector search, replacing Vault or n8n ingestion, absorbing
Chronicle or Coppermind, a Discord bot before persistent chat works, a perfect Kanban,
and clearing all technical debt. The vision also defers renaming every `crucible`
identifier; that stays true through M1, and the rename is now scheduled for M1.5.
