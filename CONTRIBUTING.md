# Contributing

The project is Hades (the package, CLIs and images still say `crucible`).

## Delivery pipeline

1. Branch from `main`. Nothing lands on `main` without a pull request.
2. Lint and test with the same definitions CI uses: `make lint` (ruff
   format and check, mypy --strict, import-linter, and the image manifest
   check) and `make test` (the unit tier, then the integration tier against
   a Postgres container); `make test PYTEST_WORKERS=0` runs serially. Run
   it with `make up` and exercise the change against the live API or `/ui`;
   describe in the PR what you saw working and what you could not exercise.
   Open the PR when the work is done, not to find out whether it works.
3. Branch CI must be green (lint, scan, test, compose-smoke are required;
   e2e-kind and images run but are not required). One internal review round
   happens before the PR opens (the orchestrator's review of the worker's
   branch). Codex reviews every PR once, automatically, and its findings get
   a disposition (fix, or an explanation) before merge; Codex is not
   re-requested after a fix. Merges go through the merge queue on `main` and
   are squashed; do not push to `main` directly.

## Kubernetes manifests

A change under `deploy/` runs `make manifests`. When it touches the workers
namespace or the provider, run `make deploy-kind` and the `e2e-kind` target.
Worker image changes: anything under `images/` changes the worker image, so
`images/manifest.env` must be regenerated with `make images` on a machine
with Docker, or the CI images job will report the digest it built instead.

## Releases

Releases are annotated `vMAJOR.MINOR.PATCH` tags on `main`. The tag is the
release; the merge is not. Pushing the tag runs the release workflow
(`.github/workflows/release.yml`): it refuses a tag that is not on `main`,
builds the service and worker images, smokes them, publishes the images and
the bundle, and creates the GitHub release. Watch that run; the tag is not
published until it finishes.

## Style

Typed Python 3.12, `ruff` formatting, `mypy --strict`, no em-dashes in
prose, comments, or commit messages. Use plain words. Use local Central
time in operator-facing text.

## Migrations

A migration that has been applied to any database, including a
developer's, is never edited. Schema changes are a new revision. Before the
first tagged release the initial revision may be squashed, only together with a
`make reset` (compose down with volumes) called out in the PR, because every
existing database is wrong after a squash. Readiness compares the live schema to
the ORM and reports "schema drift" when they differ; that check exists because
revision 0001 was once rewritten in place after it had been applied.
