# Same targets locally and in CI (18). One definition, two callers.
COMPOSE ?= docker compose
UV ?= uv

.PHONY: up dev down lint test test-unit test-integration e2e build

up: ## normal mode: postgres, migrate, crucible
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build --wait

dev: ## developer mode: postgres only; run `uv run crucible serve --all` on the host
	@test -f .env || cp .env.example .env
	$(COMPOSE) --profile dev up -d --wait

down:
	$(COMPOSE) --profile dev down

lint:
	$(UV) sync --frozen --quiet
	$(UV) run ruff format --check crucible tests
	$(UV) run ruff check crucible tests
	$(UV) run mypy crucible tests
	$(UV) run lint-imports

test: test-unit test-integration

test-unit:
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/unit -q

test-integration: ## needs Docker for postgres:16 (testcontainers) or CRUCIBLE_TEST_DATABASE_URL
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/integration -q -m integration

e2e: ## PLACEHOLDER: the Docker-provider end-to-end tier arrives in C3 (20); runs nothing yet
	@echo "e2e: placeholder, nothing to run until phase C3 (Docker provider)"

build:
	docker build -t crucible:dev .
