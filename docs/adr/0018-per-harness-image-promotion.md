# ADR 0018: Each harness has its own default worker image

Status: accepted. The operator's decision of 2026-09-25 (crucible#116), made concrete
by FDY-0120 on 2026-09-25.

## Context

Since C11 (the operator's decision of 2026-09-22) one worker image carries all four real
harnesses, and promoting it made it the default for all four at once: a promotion row
recorded one state per image (`default`, `retained`), a promotion was refused whole when
any harness it carried was outside its adapter's range, and rolling back meant promoting
the previous digest, which moved all four back together (13, 25, ADR 0011).

Configuring v0.5.5, the operator could not tell from the Images page what to do, and
asked for a line per harness with a pulldown of the images for it. Asked whether that
pulldown should still move all four, the operator decided, 2026-09-25: "Each harness can
have a different Image: Hermes 0.5.5, AGY 0.5.6, Claude 0.7.1".

## Decision

1. **Promotion is per harness.** Each harness has one default worker image, recorded in
   `harness_images` with the version of that harness the image pins. Promoting an image
   for a harness requires only that it carries that harness at a version inside the
   adapter's tested range; the other harnesses it carries are neither checked nor moved.
2. **Rollback is per harness.** The row keeps the image the last promotion replaced, and
   rolling back swaps the two, so a second rollback undoes the first. Rolling one harness
   back never moves another.
3. **Every path that picks an image reads the launching harness's own row**: a launch
   (routing's image for the selected harness), the credential probe, the harness test,
   and a login. Another harness's default never answers for this one.
4. **Existing state carries forward** (migration 0023): each harness's current default,
   the most recent `default` promotion that carried it and so what a launch resolved to,
   becomes its own default; the most recent `retained` one that carried it becomes its
   previous image. `image_promotions` is dropped; the downgrade folds the rows back into
   one row per image.
5. The admin API names the harness: `POST /v1/admin/images/{digest}/promote` and
   `POST /v1/admin/images/rollback` take `harness` in the body, and `GET
   /v1/admin/images` adds `defaults`, one row per harness with its current image, its
   previous image, and the images offered for it. The CLI is `images promote DIGEST
   --harness H` and `images rollback --harness H`; the UI is one row per harness.
6. The Images page offers release versions and `latest`, never a CI proof tag (`ci-*`,
   crucible#111), which exists to prove a build and is not a candidate.

## Alternatives considered

- **Keep one promotion for all four and show it per harness.** Rejected by the operator's
  words: each harness can have a different image.
- **Keep `image_promotions` and add a harness column back.** One row per image and
  harness is the pre-C11 shape with the images now shared; a harness's default would
  still have to be derived from the most recent row, which is what made "which image does
  Hermes run" hard to answer. One row per harness answers it directly.

## Consequences

- A harness can run a newer image than another: its launches, probes, tests and logins
  use its own. Two harnesses on different images are two sets of pulls on a node.
- The one-image build of C11 is unchanged: the release still builds one worker image
  with all four harnesses. Only the choice of default is per harness.
- ADR 0011's "an admin promotes it to default explicitly; one prior known-good image is
  retained" now holds per harness.
- The C11 implementation notes describe the previous rule and stay as history.
