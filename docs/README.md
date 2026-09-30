# Documentation index

**Operating** -- Deployment and client reference.

| File | Purpose |
| --- | --- |
| [`deployment.md`](deployment.md) | Deploying Crucible on Kubernetes |
| [`client.md`](client.md) | The `crucible` client: a reference for agents |
| [`deploy/kubernetes/secret-shapes/README.md`](../deploy/kubernetes/secret-shapes/README.md) | Secret shapes |

**Direction** -- Vision and roadmap.

| File | Purpose |
| --- | --- |
| [`vision.md`](vision.md) | Hades Roadmap and Bootstrap Directive |
| [`roadmap.md`](roadmap.md) | Hades roadmap |

**Design** -- Specifications. Each spec covers one concern; the code and ADRs win where they differ.

| File | Purpose |
| --- | --- |
| [`spec/00-overview.md`](spec/00-overview.md) | 00. Overview, scope, and non-goals |
| [`spec/01-architecture.md`](spec/01-architecture.md) | 01. Architecture and trust boundaries |
| [`spec/02-prior-art.md`](spec/02-prior-art.md) | 02. Prior-art decision record |
| [`spec/03-domain-model.md`](spec/03-domain-model.md) | 03. Domain model and ownership of authoritative state |
| [`spec/04-api.md`](spec/04-api.md) | 04. Versioned API contracts |
| [`spec/05-task-contract.md`](spec/05-task-contract.md) | 05. Task contract schema (TaskContractV1) |
| [`spec/05b-policy-schema.md`](spec/05b-policy-schema.md) | 05b. Policy schema (PolicyV1) |
| [`spec/06-worker-identity.md`](spec/06-worker-identity.md) | 06. Injected worker identity (WorkerIdentityV1) |
| [`spec/07-harness-adapters.md`](spec/07-harness-adapters.md) | 07. Harness adapter contracts |
| [`spec/08-execution-providers.md`](spec/08-execution-providers.md) | 08. Execution-provider contracts |
| [`spec/09-lifecycle.md`](spec/09-lifecycle.md) | 09. Lifecycle state machines |
| [`spec/10-events-leases-reconciliation.md`](spec/10-events-leases-reconciliation.md) | 10. Events, leases, heartbeats, and reconciliation |
| [`spec/11-definition-of-done.md`](spec/11-definition-of-done.md) | 11. Definition of done, gates, and evidence |
| [`spec/12-credentials.md`](spec/12-credentials.md) | 12. Credential and secret handling |
| [`spec/13-local-operation.md`](spec/13-local-operation.md) | 13. Local operation: Docker Compose, worker images, and the Docker security model |
| [`spec/14-persistence.md`](spec/14-persistence.md) | 14. PostgreSQL schema outline and migration strategy |
| [`spec/15-bootstrap-ledger-handoff.md`](spec/15-bootstrap-ledger-handoff.md) | 15. Foundry bootstrap ledger and authority handoff |
| [`spec/16-failure-semantics.md`](spec/16-failure-semantics.md) | 16. Failure, restart, retry, cancellation, cleanup, and retention |
| [`spec/17-notification.md`](spec/17-notification.md) | 17. Notification and Foundry-wake contract |
| [`spec/18-testing.md`](spec/18-testing.md) | 18. Testing strategy |
| [`spec/19-readiness-gate.md`](spec/19-readiness-gate.md) | 19. Crucible readiness gate |
| [`spec/20-implementation-phases.md`](spec/20-implementation-phases.md) | 20. Proposed implementation phases |
| [`spec/21-spikes.md`](spec/21-spikes.md) | 21. Technical spikes (Phase C0) |
| [`spec/22-open-questions.md`](spec/22-open-questions.md) | 22. Decisions taken and questions still open |
| [`spec/23-github-delivery.md`](spec/23-github-delivery.md) | 23. GitHub delivery: publication, PR observation, external review, CI certification |
| [`spec/24-release.md`](spec/24-release.md) | 24. Release contract and release lifecycle |
| [`spec/25-administration.md`](spec/25-administration.md) | 25. Crucible administration: admin API, `crucible admin` CLI, credential onboarding |
| [`spec/26-kubernetes-provider.md`](spec/26-kubernetes-provider.md) | 26. Kubernetes execution provider |

**Decisions** -- Architectural decision records.

| File | Purpose |
| --- | --- |
| [`adr/0001-modular-monolith-python.md`](adr/0001-modular-monolith-python.md) | ADR 0001: Modular monolith in typed Python (proposed) |
| [`adr/0002-fastapi-pydantic-sqlalchemy-alembic.md`](adr/0002-fastapi-pydantic-sqlalchemy-alembic.md) | ADR 0002: FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, pytest (proposed) |
| [`adr/0003-postgres-authoritative.md`](adr/0003-postgres-authoritative.md) | ADR 0003: PostgreSQL is the only authoritative state (proposed) |
| [`adr/0004-docker-socket-proxy.md`](adr/0004-docker-socket-proxy.md) | ADR 0004: Rootless Docker daemon preferred; restricted socket proxy (accepted) |
| [`adr/0005-container-is-the-boundary.md`](adr/0005-container-is-the-boundary.md) | ADR 0005: The execution environment is the security boundary (proposed) |
| [`adr/0006-sqlite-bootstrap-ledger.md`](adr/0006-sqlite-bootstrap-ledger.md) | ADR 0006: SQLite bootstrap ledger for Foundry until handoff (accepted) |
| [`adr/0007-github-app-and-crucible-owned-mutations.md`](adr/0007-github-app-and-crucible-owned-mutations.md) | ADR 0007: GitHub App authentication (accepted) |
| [`adr/0008-external-review-bounded.md`](adr/0008-external-review-bounded.md) | ADR 0008: External review is a bounded quality input (accepted) |
| [`adr/0009-ci-certification-escalates.md`](adr/0009-ci-certification-escalates.md) | ADR 0009: Pre-PR verification is the proof (accepted) |
| [`adr/0010-release-by-contract-and-tag.md`](adr/0010-release-by-contract-and-tag.md) | ADR 0010: Releases happen only through an operator-authorized contract (accepted) |
| [`adr/0011-harness-version-pinning-and-promotion.md`](adr/0011-harness-version-pinning-and-promotion.md) | ADR 0011: Harness versions are pinned per image (accepted) |
| [`adr/0012-admin-api-and-cli-share-services.md`](adr/0012-admin-api-and-cli-share-services.md) | ADR 0012: Administration is a versioned admin API and a CLI (accepted) |
| [`adr/0013-postgresql-only-transport.md`](adr/0013-postgresql-only-transport.md) | ADR 0013: PostgreSQL is the only transport (accepted) |
| [`adr/0014-quota-observation.md`](adr/0014-quota-observation.md) | ADR 0014: Subscription quota is advisory only (rejected) |
| [`adr/0015-service-owns-harness-credential-secrets.md`](adr/0015-service-owns-harness-credential-secrets.md) | ADR 0015: The service owns the harness credential Secrets (accepted) |
| [`adr/0016-first-run-token-never-logged.md`](adr/0016-first-run-token-never-logged.md) | ADR 0016: The first-run token is never logged (accepted) |
| [`adr/0017-service-owns-the-github-app-credential.md`](adr/0017-service-owns-the-github-app-credential.md) | ADR 0017: The service owns the GitHub App credential (accepted) |
| [`adr/0018-per-harness-image-promotion.md`](adr/0018-per-harness-image-promotion.md) | ADR 0018: Each harness has its own default worker image (accepted) |
| [`adr/0019-private-checkout-through-the-github-app.md`](adr/0019-private-checkout-through-the-github-app.md) | ADR 0019: A private repository is cloned with a read-only GitHub App token (accepted) |
| [`adr/0020-project-toolchain-in-the-worker-image.md`](adr/0020-project-toolchain-in-the-worker-image.md) | ADR 0020: A project's check toolchain rides in the worker image (accepted) |
| [`adr/0021-enabling-a-harness-is-an-administrators-decision.md`](adr/0021-enabling-a-harness-is-an-administrators-decision.md) | ADR 0021: Enabling a harness is an administrator's decision (accepted) |
| [`adr/0022-kubernetes-publisher.md`](adr/0022-kubernetes-publisher.md) | ADR 0022: On Kubernetes, the publisher is a Job (accepted) |
| [`adr/0024-review-is-the-enforcement.md`](adr/0024-review-is-the-enforcement.md) | ADR 0024: The review is the enforcement (accepted) |
| [`adr/0025-the-delivery-half-always-has-a-way-out.md`](adr/0025-the-delivery-half-always-has-a-way-out.md) | ADR 0025: The delivery half always has a way out (accepted) |
| [`adr/0028-hermes-first-routing.md`](adr/0028-hermes-first-routing.md) | ADR 0028: Hermes first in routing (accepted) |
| [`adr/0029-discard-an-import-and-rename-a-principal.md`](adr/0029-discard-an-import-and-rename-a-principal.md) | ADR 0029: Discard a verified import, skip native tasks (accepted) |

**History** -- Implementation notes and spikes.

| File | Purpose |
| --- | --- |
| [`implementation-notes/`](implementation-notes/) | Phase notes and the closest thing to a changelog |
| [`history/spikes/`](history/spikes/) | Technical spike results from Phase C0 |

**Readiness** -- Readiness evidence as of phase C7a, 2026-09-21.

| File | Purpose |
| --- | --- |
| [`readiness.md`](readiness.md) | Crucible readiness report (19)
