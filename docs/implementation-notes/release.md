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
   published v0.1.0. Compose will still build when an image is absent, because
   the services carry a `build:` section, so the step compares the running
   containers' image IDs against the freshly built one rather than trusting the
   pull policy. It then checks `/v1/ready`, asserts `/v1/health` reports the
   tagged version, and drives one task through the fake provider to `reported`.
5. **Publish.** Whether the version already exists is decided by the registry
   API, where only an explicit HTTP 404 means absent: a blip, a rate limit or an
   auth failure stops the job rather than being read as "not published". An
   existing version from this same commit is a re-run and the push is skipped;
   an existing version from a different commit means the tag was moved, which
   fails the job. Push `:latest` only when this version is the highest already
   published, so re-tagging an old fix does not move `latest` backwards.
6. **Create the GitHub release** with generated notes, last, so a release never
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

The fix pulls every service except the candidate by name before the smoke, so a
service added later is covered without anyone remembering to update a list, and
leaves `--pull never` in place so the candidate itself can still never be
fetched from the registry.

Reproduced locally before fixing. The workstation's copy of the pinned postgres
image could not be removed, because containers belonging to other projects were
using it, so the reproduction pinned a different postgres 16 digest that was
genuinely absent from the local daemon, which is the same condition:

- Before the fix: `up -d --wait --pull never` failed with the identical
  `No such image: postgres:16@sha256:...` error.
- After the fix: the supporting image is pulled, the stack comes up healthy, the
  running containers' image IDs still match the freshly built candidate, and
  `/v1/health` reports the derived version.

## Who pushes the tag

`on: push: tags` does not fire for a tag pushed with the default `GITHUB_TOKEN`.
When ADR 0010's publisher container starts cutting tags, it needs a GitHub App
installation token or a PAT, or the tag will land and no release will happen.
A tag pushed by a human from a workstation triggers the workflow normally.

## Known gaps

- The compose smoke body in `release.yml` is a copy of the one in `ci.yml`
  rather than a shared script. They will diverge the first time the task
  contract schema changes.
- No test asserts that `/v1/health` reports the derived version. The only guard
  is the release workflow's own check, which runs after the tag is public but
  before anything is published.
- `make build` still tags `crucible:dev`, which nothing consumes any more.
