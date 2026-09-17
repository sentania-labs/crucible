# ADR 0014: Subscription quota is observed through a pinned third-party reader, and is advisory only

Status: proposed, 2026-09-17. Bringing in outside prior art that opens
Crucible's dedicated credential files needs the operator's decision; this
ADR is not accepted until he takes it.

## Context

Crucible routes across three subscription-authenticated harnesses and has no
way to see how much of each subscription's window is left. Today an
exhausted window is discovered only by running into it: the harness reports
a rate or quota limit, the adapter classifies `quota_exhausted` (07), the
task ends `reported` with the class visible, and Foundry is woken to choose
another harness (16, 17). Every attempt spent that way is a wasted launch,
a wasted checkout lease, and a round trip through the orchestrator.

RoutingPolicyV1 (05b) already has the shape for something better. It carries
pools with a window and a `budget_units`, and it already checks quota twice,
advisory at submit and authoritative at attempt launch inside the fenced
transaction. What it counts is Crucible's own attempts, not what the
provider says is left, and the shipped soft limits are 0, meaning observe
only.

Spike S14 evaluated `quota-axi`, the npm reader firstmate has used against
the same three provider families for months, rather than building a reader
per harness. It answers for Codex and AGY from Crucible's dedicated
credential directories, with a `--profile-only` mode that reads exactly one
named credential file and has no default-location fallback, and it reports
"unknown" rather than a guess when a provider gives it nothing. It does not
answer for Claude Code, because Crucible's dedicated Claude credential is a
`setup-token` long-lived token (S1b) that Anthropic's usage endpoint refuses
with 403.

## Decision

Crucible observes per-harness subscription quota by invoking a pinned
`quota-axi` from the `crucible` service, and treats every reading as an
advisory observation.

- The reader runs in the service, which already holds the dedicated
  credential directories. Workers never invoke it and never see a reading.
- One scoped call per harness, on the reconcile tick, rate limited, never in
  the launch path: `--profile-only` with `CODEX_HOME` for Codex and
  `CLAUDE_CONFIG_DIR` for Claude Code, and a `HOME`-scoped call for AGY,
  each under `env -i` with a scratch `HOME` and scratch XDG roots. Never a
  combined call, because `--profile-only` takes one provider. Never
  `--full`, because only `--full` carries account identity. The operator's
  daily-use directories are never read.
- A reading is stored as an observation with its own timestamp, the tool's
  `generatedAt`, and a source naming the tool and its version. No credential
  value, account identifier, or email is stored, and the default output
  carries none.
- An observation can only make a pool **ineligible**, never eligible. A pool
  observed exhausted is refused at the authoritative launch check with the
  existing `quota_exhausted` class, and the attempt defers through 05b's
  existing `harness_launch_deferred` path, re-evaluated on each tick against
  the newest observation. Absent, stale, `unknown`, and unparseable readings
  all leave the pool eligible.
- Crucible never waits on a reset as though it were a deadline. A reset says
  when a deferred attempt is worth re-probing, nothing more. Where a
  provider supplies no reset, or no reading at all, the re-probe interval
  alone carries the deferral.
- The reader is pinned by version and by resolved dependency tree, installed
  at image build from a committed lockfile with its registry integrity hash
  recorded beside the other pinned inputs (13), and its `update` command is
  never invoked (S11's posture, applied to a non-harness dependency). A
  version bump is a dependency-update PR that re-runs S14's isolation
  evidence as its promotion check (ADR 0011's shape).
- Every reading is validated before use: version floor, `schemaVersion`, and
  full parse. A reading that does not validate is discarded whole.
- Behaviour with the reader absent or incompatible is exactly today's
  behaviour, and an integration test that removes the binary asserts it.

## Alternatives considered

**Build a per-harness reader.** Rejected for now. It would mean owning three
vendor usage endpoints, three credential-store shapes, three window
taxonomies, and the burn-rate projection maths, and keeping all of it
current against vendors who change it without notice, for a signal that is
advisory by construction and that Crucible must be able to run entirely
without. The cost is not justified by the value while the signal can only
ever remove a candidate. This stays the fallback if the dependency proves
unstable or its posture changes; the integration point is deliberately
narrow enough that swapping the producer is a contained change.

**Do nothing and keep failing into an exhausted window.** Rejected, but it
is the floor the design keeps. It costs a launch, a lease, and an
orchestrator round trip every time a window is already spent, and it gives
Foundry no way to avoid a pool it could have known was empty. It remains the
behaviour whenever no valid reading exists, which is why adopting the reader
adds no new way for work to stop.

**Read quota from the harnesses' own run output.** Partially available and
kept as a complement, not a substitute: Claude Code's stream-json carries
`rate_limit_event` lines reporting five-hour and seven-day utilization (S1).
That is a reading Crucible gets for free, on the one harness the third-party
reader cannot see, but only during a run, which is too late to route with.

## Consequences

Two of three pools gain an observed headroom signal and one does not, so
"never block on a missing reading" is a load-bearing rule rather than a
nicety, and the routing code has to be correct with the reader absent before
it is correct with it present.

A third-party package now opens Crucible's dedicated auth files and sends
those credentials to the providers' own endpoints. That is a real widening
of the credential path (12), narrowed as far as the tool allows: one
harness per call, one named credential file per call, a scoped environment,
and no cache. Crucible cannot continuously verify the tool's documented
posture, so the pin, the lockfile, and the re-run of S14's evidence on every
bump are the control.

An output contract Crucible does not own enters the routing path. The
observed track record is good, `schemaVersion: 5` and the same output
headers held across seventeen releases, but validation on every reading is
required and a schema change is expected to arrive eventually as a discarded
reading rather than as a bad decision.

`GET /routing/usage` gains an observed figure beside the counted one, with
its own age, and `GET /v1/admin/status` gains an observation age per
harness, so the operator can see when Crucible is routing blind. The wake
contract (17) is unchanged: `quota_exhausted` already wakes Foundry and
Foundry choosing another harness is already the documented response.

Spec changes this decision would require are listed in S14 and are not made
by this ADR.
