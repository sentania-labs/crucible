# Release: the tag is the trigger

How a Crucible release happens, and why it is shaped this way. Policy lives in
the lab's `sdlc` and `github-ci` skills; this note records what those mean for
this repository.

## The version comes from the tag

`pyproject.toml` declares `dynamic = ["version"]` and gets the value from
`hatch-vcs`, which reads `git describe`. There is no version constant in the
tree, so cutting a release is a tag push and never a version-bump commit on
`main`. A build with no tag in reach reports a dev version such as
`0.0.0.dev16+g2ba8818`; a build with no git at all falls back to `0.0.0.dev0`
rather than failing.

`crucible.__version__` reads the installed distribution metadata
(`importlib.metadata`), which is what `/v1/health` reports.

The Docker build context excludes `.git` (see `.dockerignore`), so the image
cannot read the tag itself. The `VERSION` build arg carries it in, and the
Dockerfile uses it for both the installed package version and the
`org.opencontainers.image.version` label. Building without the arg yields the
dev fallback, which is the right answer for a local `make build`.

## Cutting a release

```sh
git tag -a v0.1.0 -m v0.1.0 && git push origin v0.1.0
```

That push is the release. Everything after it is `.github/workflows/release.yml`,
on GitHub-hosted runners because the smoke test is `docker compose` and needs a
real Docker daemon. Each step gates the next:

1. **Refuse a bad tag.** The tag must match `vMAJOR.MINOR.PATCH` and its commit
   must be reachable from `main`. A tag on a stray branch publishes nothing.
2. **Build** `ghcr.io/sentania-labs/crucible:<version>` with `VERSION` passed in.
3. **Check the version reached the artifact**: the package version inside the
   image and the OCI version label must both equal the tag.
4. **Pull the supporting images, then smoke the exact image**, not a rebuild and
   not the published one.
   `CRUCIBLE_IMAGE` pins compose to this run's versioned local tag and
   `--pull never` forbids the registry, so a v0.2.0 run cannot boot the
   published v0.1.0. `tools/release/compose_images.py` resolves the Compose
   model as JSON with every profile active. It identifies candidate services by
   comparing each resolved image with `CRUCIBLE_IMAGE`, pulls only the other
   services, and checks every candidate service container against the freshly
   built image ID. The container lookup includes exited one-shot services.
   Release boots with
   `make up COMPOSE_UP_FLAGS="--pull never --no-build"`, so it runs the same
   `preflight` and generated proxy-config path as local work and CI without
   pulling or rebuilding over the candidate before it then runs `make smoke`,
   which is
   `tools/smoke/compose_smoke.py`, the same file CI runs on every pull request.
5. **Prove and push the worker images** (C11). `make images-check NO_CACHE=1`
   builds the worker image (all four harness CLIs) and the script-harness image
   from scratch with the same `tools/images/images.sh` that `make images` and
   CI's `images` job run, and fails unless every tag, harness version and OCI
   digest equals `images/manifest.env`. `tools/release/push_worker_images.sh`
   then pushes them with `docker push` as
   `ghcr.io/sentania-labs/crucible-worker:<version>` and
   `:script-harness-<version>`, the way the service image and every ScarGuard
   service are pushed (the operator's decision, 2026-09-23, which retired a
   hand-written registry uploader and the CI job that proved it on GHCR). Only
   buildx's own "not found" is absent; a tag already there from the same
   `crucible.build_inputs` is a re-run and is skipped; any other answer stops
   the release. This runs before the service version tag is pushed.
6. **Publish.** Whether the version already exists is decided by the registry
   API, where only an explicit HTTP 404 means absent: a blip, a rate limit or an
   auth failure stops the job rather than being read as "not published". An
   existing version from this same commit is a re-run and the push is skipped;
   an existing version from a different commit means the tag was moved, which
   fails the job. Move `latest` only when this version is the highest already
   published, so re-tagging an old fix does not move `latest` backwards. All
   three images' `latest` (the service image, `crucible-worker`'s, and
   `script-harness-latest`) move the same way in the same step: copied on the
   registry from each one's own just-confirmed version tag, never pushed from
   this job's local build, so a re-run that skips the version push still
   leaves every `latest` on a digest a version tag also carries (2026-09-23).
   The release notes then read every digest back with
   `docker buildx imagetools inspect`.
7. **Create the GitHub release** with generated notes, last, so a release never
   points at a version that is not consumable from the registry.

The workflow uses the job's `GITHUB_TOKEN` with `packages: write` and
`contents: write`. No secrets are configured for it.

## Defaults track latest

`compose.yaml` names `ghcr.io/sentania-labs/crucible:latest`. Deployments pin an
exact tag and that pin lives in the deployment repository, which is what keeps
this repository free of version-bump commits.

`CRUCIBLE_IMAGE` overrides that reference and exists for the release workflow,
which uses it to boot one exact locally built candidate.

Use `make up` for local work. It passes `--build`, so the working tree is what
runs. A bare `docker compose up` pulls the published `latest` when one exists
and only builds if that pull fails, so once v0.1.0 is out it would silently
exercise the release instead of your change.

## First live run: the fresh-runner pull trap

The first live run of this workflow, on tag `v0.1.0` (run `35158679884`), failed
at the smoke step with:

```
Container crucible-postgres-1  Error response from daemon: No such image:
postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94
```

`docker compose up --pull never` applies the pull policy to **every** service,
not just the release candidate. A GitHub-hosted runner is a clean machine with
no image cache, so the digest-pinned `postgres:16` was never fetched and compose
refused to create the container. It passed in local testing only because
postgres was already on the workstation, which is exactly the class of
assumption a fresh runner exists to catch.

The gates behaved correctly: the smoke failed, the publish and release steps
were skipped, nothing reached GHCR and no GitHub release was created. The cost
was a burnt tag, not a bad artifact.

The initial fix pulled every service except a hand-written candidate service
list before the smoke and left `--pull never` in place. That repaired the fresh
runner failure, but the service-name exclusion later drifted when another
service began using the candidate image. The third live run below replaces the
name list with resolved-image classification.

Reproduced locally before fixing. The workstation's copy of the pinned postgres
image could not be removed, because containers belonging to other projects were
using it, so the reproduction pinned a different postgres 16 digest that was
genuinely absent from the local daemon, which is the same condition:

- Before the fix: `up -d --wait --pull never` failed with the identical
  `No such image: postgres:16@sha256:...` error.
- After the fix: the supporting image is pulled, the stack comes up healthy, the
  running containers' image IDs still match the freshly built candidate, and
  `/v1/health` reports the derived version.

## Second live run: the smoke that only existed in two places

The v0.2.0 run (`35167333375`) failed at the smoke step. The task submit
returned HTTP 422 and the step died inside `json.load`, parsing an empty body,
so the log showed a traceback and not a reason.

The reason was drift. `release.yml` carried its own copy of the compose smoke,
written against the C1 task contract: no `execution_request.tier`, a `model`
that is not a routing entry, a `pull_request` deliverable, and a wait for the
`reported` state. C2 changed the contract and the lifecycle and updated the copy
in `ci.yml` only. Every pull request in C2 was green, because CI was exercising
the corrected copy while the release path still held the stale one. The
duplication is what made a correction-without-rerunning-every-consumer possible;
the 422 was only the symptom.

Same outcome as the first run: the gates held, nothing was published, no release
was created, the cost was a burnt tag.

The fix is one definition. `tools/smoke/compose_smoke.py` is the whole smoke,
`make smoke` runs it, and both workflows call that target. CI's `compose-smoke`
job runs it with the same environment as the release path: the release step adds
`CRUCIBLE_IMAGE`, which pins compose to the candidate, and
`CRUCIBLE_SMOKE_EXPECT_VERSION`, which asserts `/v1/health` reports the tag.
Nothing else differs, so a contract change that would break the release now
fails the pull request that introduces it.

The script reports the method, URL, HTTP status and response body on any
non-2xx, so the next contract mismatch reads as a 422 carrying the server's
validation error instead of a JSON traceback. A missing field in a response is
reported the same way rather than as a `KeyError`, a task that lands in a dead
end such as `blocked` or `pre_pr_gates_failed` fails immediately instead of
waiting out the poll budget and reporting as stuck, and the output of
`crucible admin token create` is never echoed into a failure message, because a
failed release run is a public log.

The release path drives an `artifacts` deliverable to `accepted`, where its old
copy drove a `pull_request` deliverable to `reported`. That is deliberate: in C2
only an `artifacts` deliverable reaches `accepted`, so the shared smoke exercises
the longer path, through gate evaluation, a non-author internal review and
acceptance, rather than stopping at the report.

The release workflow keeps the guards that are genuinely release-only: the
supporting-image pre-pull, `--pull never`, the running-container image ID
assertion, and the fail-closed GHCR manifest check.

## Third live run: a candidate service looked like supporting infrastructure

The v0.3.0 run (`35638978974`) failed before Compose booted. C7a added the
one-shot `credential-init` service on `${CRUCIBLE_IMAGE}`, but the release
workflow's candidate exclusions still named only `crucible` and `migrate`.
The supporting-image loop therefore asked GHCR for the unpublished `0.3.0`
candidate and received `manifest unknown`. Nothing was published.

The fix has one classifier in `tools/release/compose_images.py`. Both the pull
operation and the post-boot identity check consume its resolved Compose image
classification. The release workflow calls it through Make targets, and the CI
compose-smoke job runs its classify-only mode before boot. A unit test fixes the
current candidate set at `credential-init`, `crucible`, and `migrate`, and also
requires a service without an image to fail with its service name.

This closes the local and CI divergence: release alone had a service-name pull
list, while local and CI only exercised normal Compose boot; all three now use
the shared resolved-image classifier before a tag can publish.

## Who pushes the tag

`on: push: tags` does not fire for a tag pushed with the default `GITHUB_TOKEN`.
When ADR 0010's publisher container starts cutting tags, it needs a GitHub App
installation token or a PAT, or the tag will land and no release will happen.
A tag pushed by a human from a workstation triggers the workflow normally.

## Readiness uses CI from the tagged commit

The release workflow does not rerun the repository test tiers. The release job
builds, verifies, smokes, and publishes one tagged artifact. Repeating the CI
test matrix there would create two independent definitions and recreate the
drift that caused the v0.2.0 failure.

For readiness row 14, "all tiers except live" means the five jobs in
`.github/workflows/ci.yml`: `lint`, `scan`, `test`, `e2e`, and
`compose-smoke`. The evidence is the green `ci` run attached to the release
tag's commit, not the release workflow itself.

The current release, v0.2.1, points to
`a7a23679b85161982947abf49f5254cb6bf6d8eb`. Its green `ci` run is
https://github.com/sentania-labs/crucible/actions/runs/35169303005. That commit
predates the addition of the `e2e` job, so its historical run has four jobs.
The current five-job shape is green on `main` at
https://github.com/sentania-labs/crucible/actions/runs/35527704824. The next
release tag based on the current workflow will have the complete five-job
evidence without a second test execution in `release.yml`.

## Known gaps

- No test asserts that `/v1/health` reports the derived version. The only guard
  is the release workflow's own check, which runs after the tag is public but
  before anything is published.
- `make build` still tags `crucible:dev`, which nothing consumes any more.
