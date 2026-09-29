# ADR 0028: Hermes first in routing; frontier by intent; demotion that recovers

Status: accepted. The operator's direction of 2026-09-29 ("We need to ensure that Hermes
is not the anti-route. It should probably be close to our default doer with frontier
being hard structural problems for scoping of items for hermes/qwen."), with the same
day's stance that review is the enforcement and hard failures belong only where the
damage is real. Built by FDY-0142 on 2026-09-29. Amends the selection rule of spec 05b.

## Context

Within a tier, candidates ranked by capability preference, then demotion, then
least-recently-used rotation. Nothing preferred the local pool: once Claude Code and
AGY were signed in, Hermes got a share of the work, not the default. And one failed gate
or correction in a model's last 20 attempts on a project demoted it, with no way back:
a demoted model got no attempts, so its record never improved, and only an operator pin
could move it.

## Decision

1. **A preferred pool order per tier.** `tiers.<tier>.prefer_pools` in the routing
   policy lists pools in the order routing tries them, ahead of the capability
   preference. Models outside those pools are fallbacks, selected when every preferred
   one is excluded: disabled, harness disabled or without a credential, pool at its soft
   limit, or pool marked exhausted.
2. **The default is Hermes first for routine work.** A tier without `prefer_pools` reads
   the pools that hold a `local` model first for `trivial` and `standard`, and has no
   pool preference otherwise, so `complex` goes to its preferred capability, frontier.
3. **Endpoint down is a fallback case.** A worker on a `local` model whose exit is
   `provider_error` (the gateway refused, was unreachable, or answered 5xx) marks its
   pool for the pool's `default_cooldown_seconds`. The mark is the quota mark's row:
   listed on the Routing page and clearable there with a reason.
4. **Demotion judges a rate.** Over the model's last `quality_window` attempts on the
   project, only attempts that reached the gates are judged, and only a failed blocking
   gate is a failure: `gates_failed` counts blocking gates, so an advisory finding
   (ADR 0024) never counts, and corrections no longer count. A model is demoted when
   at least `demote_min_sample` (default 5, at least 2) were judged, at least two
   failed, and failures are at least `demote_failure_percent` (default 50) of them. One
   failure never demotes.
5. **A demoted model recovers.** Once its last attempt is `probe_after_minutes`
   (default 60) old it ranks as if not demoted, for one attempt: that launch makes its
   last attempt new again. Passing probes lower its rate until it is no longer demoted;
   the window sliding past old failures does the same.
6. **Frontier by intent is the `complex` tier.** No new contract field: the orchestrator
   asks for scoping ("scope this into Hermes-sized tasks") or structural work by
   submitting it as `complex`, and its deliverable is the smaller contracts it then
   submits as `trivial` or `standard`.
7. **Every setting has the three surfaces.** `GET`/`POST /v1/admin/routing/preference`,
   `crucible admin routing preference|set-preference`, and the Routing page (an "In
   force" line for the order and one for demotion, the view under Details, and an edit
   form). A save writes a new routing version and a delivery policy version naming it,
   audited as `routing_policy_uploaded` and `policy_uploaded` with the reason.

## Upgrade

No stored version is rewritten and no migration is needed. The new fields are optional,
and a version without them reads their defaults when it is loaded, so the routing
version in force on the lab routes Hermes first for `trivial` and `standard` as soon as
the new code runs. A task that references an older version routes the same way on its
next launch or reroute, which is the point of the change; its stored document is
untouched. The Routing page marks a tier that reads the default as "(default)".

## Consequences

- With Hermes enabled and its gateway up, routine work stops rotating across the
  subscriptions; the subscriptions carry it only when Hermes is unavailable or demoted.
- A flaky gateway moves routine work to subscriptions for the lab-local pool's cooldown
  (3600 seconds as seeded). Clearing the mark on the Routing page brings it back at once.
- `gates_failed` in `GET /routing/history` now counts blocking failures only.
- Until ADR 0024 lands every gate is blocking, so every failed gate counts.
