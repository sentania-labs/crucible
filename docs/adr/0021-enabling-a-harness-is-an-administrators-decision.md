# ADR 0021: Enabling a harness is an administrator's decision; configuration is the default

Status: accepted. The operator's decision recorded in hades #174 (2026-09-27: "how do I
enable the codex harness, the settings page seems read only", and the stance of the same
day that the app should assume access and report clearly when something does not work),
built by FDY-0132 on 2026-09-28. Amends spec 25's two-gate enablement.

## Context

A harness had two gates and a launch needed both: the configuration entry
`harnesses.<name>.enabled` (on Kubernetes, `CRUCIBLE_HARNESSES__<NAME>__ENABLED` in the
settings ConfigMap) and the administrator's runtime flag. Codex ships with the
configuration gate closed ("unverified: Crucible-side refresh not yet observed", S1b),
so turning it on meant a GitOps edit and a restart, and the Settings page showed that
gate read-only. That broke the rule that every tunable has a UI.

## Decision

1. **The configuration entry is the starting value.** Until an administrator has
   decided, a launch needs the configuration entry and the runtime flag both, as
   before.
2. **An administrator's enable or disable is stored and then alone decides.** The
   harness's row records that a decision was made (`harnesses.enabled_decided`,
   migration 0027). The Harnesses page, `POST /v1/admin/harnesses/{name}/enable|disable`
   and `crucible admin harnesses enable|disable` all make it, through the same service,
   audited as `harness_enabled` or `harness_disabled` with the principal, the reason
   and the before-and-after summary. Routing reads the row on every submit and launch,
   so the decision holds for new tasks at once, with no restart.
3. **Unverified is a warning, not a lock.** The configuration's reason is reported as
   `warning` wherever the harness is shown and in the enable answer, and recorded with
   the decision as `configuration_warning`. The harness test (crucible#118) is how the
   operator proves the harness works.
4. **An upgrade changes nothing by itself.** Migration 0027 records a row an
   administrator had disabled as that decision, and starts every enabled row undecided,
   so a harness the configuration keeps off stays off until an administrator enables it.
5. **Removing a credential is a decision too.** `credentials remove` disables the
   harness through the same service, so the harness stays off, whatever its
   configuration says, until an administrator enables it again.

## Consequences

Changing a harness's configuration entry after an administrator has decided does not
change that harness's availability; the Settings page says the entry is the starting
value only. A rollback past 0027 drops the decision, and the two gates apply again.

The harness test runs only on an enabled harness, so an unverified harness is enabled
for new tasks before the test can prove it. Letting the test run on a harness that is
off is a possible follow-up.
