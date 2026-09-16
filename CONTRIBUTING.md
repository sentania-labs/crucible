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
