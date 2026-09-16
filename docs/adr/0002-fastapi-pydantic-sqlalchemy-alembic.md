# ADR 0002: FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, pytest

Status: proposed, 2026-09-16.

## Context

The API must be versioned with generated OpenAPI; contracts must be
validated and versioned; persistence must have explicit migrations; tests
must run without a model subscription.

## Decision

FastAPI for `/v1` (OpenAPI for free, async where the supervisor benefits).
Pydantic v2 for every contract and for settings. SQLAlchemy 2.x typed ORM
with hand-written Alembic migrations (autogenerate only as a draft). pytest
with `testcontainers` for PostgreSQL in the integration tier.

## Alternatives considered

Litestar or Starlette alone: fewer conventions, no gain. Raw `asyncpg` with
SQL files: faster, but migrations and typed rows would be hand-rolled, and
the domain would leak SQL. Django: heavier than the problem, and its ORM
pushes lifecycle logic into models.

## Consequences

Contract types are the single source for validation, storage documents,
and API docs. Migrations are reviewed artifacts. The supervisor loop uses
asyncio for provider polling and log pulls; database access is through
sync SQLAlchemy sessions in a thread executor in v0.x for simplicity, to be
revisited if the tick budget is exceeded.
