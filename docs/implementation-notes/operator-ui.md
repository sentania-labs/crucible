# Operator UI: per-harness images, optional reasons, harness test, density (FDY-0120)

Implemented 2026-09-25 on `feat/operator-ui` for crucible#115, #116, #117, #118, #124,
#125, #126 and #127: the operator's feedback from configuring v0.5.5, and Foundry's walk
of the same UI.

## Operator decisions

- 2026-09-25: "Each harness can have a different Image: Hermes 0.5.5, AGY 0.5.6, Claude
  0.7.1". Promotion is per harness (ADR 0018). This replaces the C11 rule that promoting
  the worker image switched all four harnesses; the C11 notes stay as the history of that
  rule. Migration 0023 carries each harness's current default forward.
- 2026-09-25: "I shouldn't have to provide a reason for everything." A reason is an
  optional audit note; bootstrap commit, repository remove, token revoke and credential
  remove still require one; read-only checks never ask (25, 04).
- 2026-09-25: pages were too busy, and where a value is known the UI offers it.

## What changed

- **Images** (#116): one row per harness with its current image, its previous image, a
  pulldown of the images that carry it at a supported version (releases and `latest`,
  never a `ci-*` proof tag), Promote and Roll back. `harness_images` replaces
  `image_promotions`; launches, probes, logins and the harness test read the launching
  harness's own row.
- **Reasons** (#117): one guard decides, `guard_mutation(..., reason_required=)`; the UI
  marks each form's reason required, optional, or absent from one table; the CLI takes
  `--reason` before or after the verb; `next` lists it under `optional` where it may be
  left out.
- **Harness test** (#118): `POST /v1/admin/harnesses/{name}/test`, `crucible admin
  harnesses test NAME`, and a Test button on each Harnesses row. Six steps in order:
  enabled, worker image, credential, model, worker starts, model call. The worker run is
  the bounded probe with every harness in a worker (Hermes included) and the model's
  local endpoint passed through, so its egress is the task's. The last result is kept on
  the harness row (`harnesses.last_test`, migration 0024).
- **Test fixtures** (#124): `test_fixtures` (`CRUCIBLE_TEST_FIXTURES`), false by default,
  wires the fake provider and registers the script harness. The compose smoke (CI and
  release) and the kind overlay turn it on. It is a restart-bound setting like its peers,
  shown on Settings.
- **Kubernetes** (#125): rotate, remove and the prepared-directory field are not offered
  where credentials are service-owned Secrets; the login link is not offered for Hermes or
  a harness that needs no credential; Settings leaves out a disabled provider's settings
  (its `enabled` row stays) and the credential directory settings Secrets replace.
- **Audit redaction** (#126): a field is hidden when its own name says it holds a
  credential, compared whole or by a credential suffix, never as a substring; harness names
  are never secret. `claude_code` was hidden because it ends in `_code`.
- **Row actions** (#127): Revoke on a principal's row, Remove on a repository's, Log on an
  attempt's, Show and Commit on an import's, Promote and Roll back on a harness's. No form
  asks for an ID to be typed.
- **Density** (#115): navigation grouped (Set up, Work, Admin), Retention and Bootstrap
  shown once they have content; Status leads with the to-do list and one service table,
  supervisor internals and provider checks behind Details; Tasks lists what needs
  attention in plain words; Harnesses is one row per harness; Settings leads with what the
  deployment set and puts the defaults behind a click; Audit shows who, what and why with
  the payload behind Details; plain lists lose their stray "Value" header.

## Left for FDY-0117

Routing, Repositories (beyond the row Remove #127 asked for), GitHub, the Hermes key on
Credentials, and the Status to-do list are being reworked by FDY-0117 (branch
`feat/first-run-setup`). Their reason fields follow the central rule without an edit to
those pages; their density was left for after that branch merges.

## Proof

`tools/kind/deploy-kind.sh` on a disposable cluster (`crucible-deploy-fdy0120-2882658`),
then, on the same deployment: two images promoted to two harnesses and one rolled back
alone; the Test action passing for the script harness against a stub model server behind
the local endpoint (the model call went from the worker Pod through its egress policy),
and failing at "Credential" for Hermes with no key.
