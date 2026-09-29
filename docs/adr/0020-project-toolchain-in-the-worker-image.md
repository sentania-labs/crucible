# ADR 0020: A project's check toolchain rides in the worker image; its dependencies come from PyPI at run time

Status: accepted. FDY-0131, 2026-09-28, on the operator's go the same evening. Answers
hades #184 for this repository and is the first part of M1a (#85). Amends nothing; the
sidecar services and the branch CI gate of #85 are still to come.

## Context

default-software requires `make lint`, `make test` and `make scan` of every task, and
Crucible re-runs them in a verifier Pod from the collected tree, in the worker image
(11, 26). For this repository those targets need `uv` (and through it ruff, mypy,
import-linter and the locked dependencies), Python 3.12, `gitleaks`, and, for the
integration tier, Postgres and a Docker daemon. The worker image had `make` (#181) and
none of the rest, and worker egress reaches no package index unless the policy names
one, so every task against this repository failed `verification_ran` whatever the work
was. #184 asked who supplies the tools behind each repository's checks: the shared
image, a per-project image, or the repository.

## Decision

1. **The toolchain is in the shared worker image.** `images/worker/Dockerfile` carries
   `uv` 0.10.12, CPython 3.12.13 (python-build-standalone, the build uv itself installs)
   and `gitleaks` 8.30.1, each its publisher's release archive pinned by version and by
   the sha256 its publisher lists, in `images/pins.env` beside every other build input.
   Python 3.12 is there because Debian bookworm ships 3.11 and this repository requires
   3.12; it is on PATH only as `python3.12`, so `python3` stays Debian's and nothing
   else changes. The image sets `UV_PYTHON_DOWNLOADS=never` (no worker reaches the host
   a Python download comes from) and `UV_LINK_MODE=copy` (uv's cache is on the
   memory-backed home, the project's `.venv` on the workspace). One image stays one
   digest to build, publish and roll back (C11); a per-project image is not needed for
   three binaries.
2. **The project's dependencies come from PyPI at run time, hash-locked (option a).**
   `uv sync --frozen` installs exactly what `uv.lock` names and checks every file
   against the lock's hash, in the worker and again in the verifier. The project's
   policy keeps `pypi.org` and `files.pythonhosted.org` in `network.egress_allowlist`;
   the Kubernetes provider turns them into per-address NetworkPolicy rules and pins both
   names to those addresses with `hostAliases` (#191), for the worker and the verifier
   alike, and never gives either github.com. Option (b), a uv cache built into the image
   from this repository's `uv.lock`, was not taken: it would tie the shared image's
   build inputs to one project's lock, so every dependency bump would mean a new worker
   image, a release and a promotion before a task could verify, and it would still need
   (a) whenever the lock had moved. The cost of (a) is a download per Pod: on the kind
   proof the whole of `make lint`, uv sync from PyPI included, took 13 seconds in the
   verifier.
3. **This repository's tasks run under their own policy, `hades-self-hosting`.** It is
   default-software with `repository.required_checks` of `make lint`, `make test-unit`
   and `make scan`: what runs in the worker image with no Docker daemon and no Postgres.
   The integration and e2e tiers need both, and branch CI on the pushed head stays the
   full proof of record (the operator's decision, 2026-09-28); `ci_certification` is
   unchanged, so a task still waits for that run. The policy ships as
   `examples/policies/hades-self-hosting.yaml` and is uploaded through the existing
   policies API; no migration seeds it.
4. **A policy may declare the programs its checks call.** `repository.required_programs`
   (05b) names what the checks need beyond their first word. Crucible never reads it at
   run time; `make images-policy-check` proves each one resolves in the worker image,
   as it already did for the first word of every required check (#181). The
   self-hosting policy declares `make`, `git`, `uv`, `python3.12`, `gitleaks`, `bash`,
   `jq`, `tar` and `sha256sum`.
5. **The unit tier runs anywhere the image does.** Two cases that rendered compose with
   the Docker CLI skip without it, the way the manifest cases skip without kubectl, and
   CI sets `CRUCIBLE_COMPOSE_REQUIRED=1` so they can never skip there. `pgrep` gave way
   to a read of `/proc`, and a mode check tolerates the setgid bit a Pod's fsGroup
   volume gives new directories. `-n auto` counts the Pod's CPU quota rather than the
   node's CPUs.

## Consequences

- A task against this repository verifies in the worker image, and the verifier's
  evidence records each check's wall-clock seconds (`verification_run.seconds`).
- The policy names a routing policy version, like every policy. When the operator
  publishes a new routing version from the admin UI, only default-software follows it;
  this policy needs a new version uploaded the same way (docs/deployment.md).
- Another Python project on uv needs no image change. A project with other tools either
  declares them and adds them to the image, or waits for a per-project image; that
  choice is still per project, as #184 put it.
- Postgres-backed tests do not run in a worker until the sidecar half of #85 lands.
