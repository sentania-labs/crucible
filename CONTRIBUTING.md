# Contributing

Crucible is in specification. Until implementation begins, contributions are
review comments on the specification, filed as issues.

Once code exists, the delivery pipeline is:

1. Branch from `main`. Nothing lands on `main` without a pull request.
2. Lint and test with the same definitions CI uses: `make lint` and
   `make test`. If local and CI disagree, that is a defect to fix in the repo.
3. Run it: `docker compose up` and exercise the change against the live API.
   Say in the PR what you saw working, and what you could not exercise.
4. Open the PR when the work is done, not to find out whether it works.
5. Address one round of review, then merge.

Releases are annotated `vMAJOR.MINOR.PATCH` tags on `main`. The tag is the
release; the merge is not.

Style: typed Python 3.12, `ruff` formatting, `mypy --strict`. No em-dashes in
prose, comments, or commit messages.

Migrations: a migration that has been applied to any database, including a
developer's, is never edited. Schema changes are a new revision. Before the
first tagged release the initial revision may be squashed, only together with a
`make reset` (compose down with volumes) called out in the PR, because every
existing database is wrong after a squash. Readiness compares the live schema to
the ORM and reports "schema drift" when they differ; that check exists because
revision 0001 was once rewritten in place after it had been applied.

Integration tier: the ``make test-integration`` target runs the integration suite
with ``-n auto`` (pytest-xdist) by default.  With xdist the integration
``database_url`` fixture starts a single Postgres container shared by all workers
and each worker creates its own database on that container, so a cold image pull
lands inside the first test's time limit.  A serial run (``-n 0`` or no ``-n``)
behaves identically to before: one container, one database.  The shared container
is stopped when the last worker finishes.
