# ADR 0014: Subscription quota is observed through a pinned third-party reader, and is advisory only

Status: proposed, 2026-09-17.

## Context

Crucible routes across three subscription-authenticated harnesses and has no
way to see how much of each subscription's window is left. Today an
exhausted window is discovered only by running into it: the harness reports
a rate or quota limit, the adapter classifies `quota_exhausted` (07), the
task ends `reported` with the class visible, and Foundry is woken to choose
another harness (16, 17). Every attempt spent that way is a wasted launch, a
wasted checkout lease, and a round trip through the orchestrator.

RoutingPolicyV1 (05b) already has the shape for something better. It carries
pools with a window and a `budget_units`, and it already checks quota twice,
advisory at submit and authoritative at attempt launch inside the fenced
transaction that moves the attempt to `launching`. What it counts is
Crucible's own attempts, not what the provider says is left, and the shipped
soft limits are 0, meaning observe only.

Spike S14 evaluated `quota-axi`, the npm reader firstmate has used against
the same three provider families since at least late July, rather than
building a reader per harness. It answers for Codex and AGY from Crucible's
dedicated credential directories. Its `--profile-only` mode reads exactly
one named credential file and, as S14's decoy runs establish, does not fall
back to a home-directory store even when a readable one is sitting there.
It reports "unknown" rather than a guess when a provider gives it nothing.
It does not answer for Claude Code, because Crucible's dedicated Claude
credential is the `setup-token` long-lived token S1b recorded: the file the
mode wants does not exist, and the usage endpoint answers the token with
403.

Bringing in outside prior art that opens Crucible's dedicated credential
files is the operator's decision, which is why this ADR is proposed rather
than accepted.

## Decision

Crucible observes per-harness subscription quota by invoking a pinned
`quota-axi` from the `crucible` service, and treats every reading as an
advisory observation.

- The reader runs in the service, which already holds the dedicated
  credential directories, mounted read-only (12). Workers never invoke it
  and never see a reading.
- One scoped call per harness, on the reconcile tick, rate limited, never in
  the launch path, each under a sanitized environment with a scratch `HOME`
  and scratch XDG roots: `--profile-only` with `CODEX_HOME` for Codex, and a
  `HOME`-scoped call for AGY. Never a combined call, because
  `--profile-only` takes one provider. Never `--full`, because only `--full`
  carries the `account` object with `accountId` and `email`. An AGY reading
  whose source is the loopback fallback rather than the CLI is discarded,
  because on a shared host that fallback can read the operator's own running
  session instead of Crucible's.
- **Claude Code is not observed** and no call is made for it. Its credential
  shape yields nothing, and inventing a reading for it is worse than having
  none. Revisit only if that credential shape changes.
- A reading is stored as an observation with its own timestamp, the tool's
  `generatedAt`, and a source naming the tool and its version. No credential
  value, account identifier, or email is stored; the default output carries
  none.
- Which observation bounds which pool is explicit configuration, never
  inferred from names. S14 records why: AGY's `claude_gpt` scope is
  Antigravity's own Claude and GPT allowance and is not `anthropic-sub` or
  `openai-sub`, despite reading like both. An observation also carries its
  own window identity, because a pool's declared window and the window the
  provider actually reports need not agree.
- An observation can only make a pool **ineligible**, never eligible.
  Absent, stale, `unknown`, and unparseable readings all leave the pool
  eligible.
- A pool observed exhausted causes the attempt to defer **before launch**,
  under its own reason, releasing or never taking the checkout lease, and is
  re-evaluated on each tick against the newest observation. This is
  deliberately not 05b's `harness_launch_deferred`, which is checked after
  the checkout lease and clears when a running attempt finishes; a quota
  deferral clears when a provider window refills, which can be days. It is
  also deliberately not the `quota_exhausted` exit class, which means a
  harness ran and reported a limit, is terminal for the attempt, and wakes
  Foundry. That class keeps its meaning (16) unchanged.
- Crucible never waits on a reset as though it were a deadline. A reset says
  when a deferred attempt is worth re-probing, nothing more, and on an
  untouched window it is not even a real clock. Where a provider supplies no
  reset, or no reading at all, the re-probe interval alone carries the
  deferral.
- The reader is pinned by version and by resolved dependency tree, installed
  at image build from a committed lockfile with its registry integrity hash
  recorded beside the other pinned inputs (13), and its `update` command is
  never invoked (ADR 0011's posture, applied to a non-harness dependency). A
  bump is a dependency-update PR that re-runs S14's decoy and watch evidence
  as its promotion check.
- Every reading is validated before use: version floor, `schemaVersion`, and
  full parse. A reading that does not validate is discarded whole.
- Behaviour with the reader absent or incompatible is exactly today's
  behaviour, and an integration test that removes the binary asserts it.
- The first real `exhausted_now` observation is a recorded checkpoint before
  the signal is allowed to refuse a launch. S14 never saw one, and it is the
  only reading that changes any behaviour.

Whether AGY is observed at all is left open for the operator. Its reading
requires the `agy` CLI inside the service container, which is a second,
separately versioned copy of a harness CLI in the process that holds every
credential, outside the per-image pinning scheme of ADR 0011. Codex alone
carries most of the value at none of that cost.

## Alternatives considered

**Build a per-harness reader.** Rejected for now. It would mean owning three
vendor usage endpoints, three credential-store shapes, three window
taxonomies, and the burn-rate projection maths, and keeping all of it
current against vendors who change it without notice, for a signal that is
advisory by construction and that Crucible must be able to run entirely
without. The cost is not justified while the signal can only ever remove a
candidate. It stays the fallback if the dependency proves unstable or its
posture changes; the integration point is deliberately narrow enough that
swapping the producer is a contained change.

**Do nothing and keep failing into an exhausted window.** Rejected, but it
is the floor the design keeps. It costs a launch, a lease, and an
orchestrator round trip every time a window is already spent, and it gives
Foundry no way to avoid a pool it could have known was empty. It remains the
behaviour whenever no valid reading exists, which is why adopting the reader
adds no new way for work to stop.

**Read quota from the harnesses' own run output.** Partially available and
kept as a complement, not a substitute: Claude Code's stream-json carries
`rate_limit_event` lines reporting five-hour and seven-day utilization (S1).
That is a reading Crucible gets for free on the one harness the third-party
reader cannot see, but only during a run, which is too late to route with.

## Consequences

Two of three pools gain an observed headroom signal and one does not, so
"never block on a missing reading" is load-bearing rather than polite: the
routing code has to be correct with the reader absent before it is correct
with it present.

A third-party package now opens Crucible's dedicated auth files and sends
those credentials to the providers' own endpoints. That is a real widening
of the credential path (12), narrowed as far as the tool allows: one harness
per call, a sanitized environment, no `--full`, no cache under
`--profile-only`, and the existing read-only mount of the credential
directory, which also refuses any write the tool might attempt. Crucible
cannot continuously verify the tool's documented posture, so the pin, the
lockfile, and the re-run of S14's evidence on every bump are the control.
Note that the narrowing is uneven: Claude and Codex get `--profile-only`,
AGY has no such mode and reads no credential file at all, shelling out to
its vendor CLI instead.

An output contract Crucible does not own enters the routing path. The
observed track record is good, `schemaVersion: 5` and the same output
headers across seventeen releases, but validation on every reading is
required, and a schema change is expected to arrive eventually as a
discarded reading rather than as a bad decision.

A new pre-launch deferral is a genuinely new behaviour, not a reuse. It
needs its own reason, its own interaction with the checkout lease, and a
decision in 17 about whether it wakes Foundry. S14 lists the specification
changes; this ADR does not make them.

`GET /routing/usage` gains an observed figure beside the counted one, with
its own age, and `GET /v1/admin/status` gains an observation age per
harness, so the operator can see when Crucible is routing blind.
