# Same targets locally and in CI (18). One definition, two callers.
COMPOSE ?= docker compose
UV ?= uv

.PHONY: up dev down reset lint scan scan-tree scan-history smoke test test-unit test-integration e2e build

up: ## normal mode: postgres, migrate, crucible
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build --wait

dev: ## developer mode: postgres only; run `uv run crucible serve --all` on the host
	@test -f .env || cp .env.example .env
	$(COMPOSE) --profile dev up -d --wait

down:
	$(COMPOSE) --profile "*" down

reset: ## DESTRUCTIVE: down plus the postgres and artifact volumes; the only cure for schema drift
	$(COMPOSE) --profile "*" down --volumes

lint:
	$(UV) sync --frozen --quiet
	$(UV) run ruff format --check crucible tests tools/smoke
	$(UV) run ruff check crucible tests tools/smoke
	$(UV) run mypy crucible tests tools/smoke
	$(UV) run lint-imports

scan: scan-tree scan-history ## secret scan; needs gitleaks on PATH

scan-tree: ## every tracked file as it is in the working tree (caches and .venv excluded)
	@T=$$(mktemp -d) && git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$$T" \
	  && gitleaks detect --no-git --redact --no-banner --source "$$T"; S=$$?; rm -rf "$$T"; exit $$S

scan-history: ## commits in SCAN_RANGE (default origin/main..HEAD)
	gitleaks detect --redact --no-banner --source . --log-opts="$${SCAN_RANGE:-origin/main..HEAD}"

smoke: ## drive one task end to end through a running stack; `make up` first
	@test -f .env || cp .env.example .env
	COMPOSE="$(COMPOSE)" $(if $(CRUCIBLE_IMAGE),CRUCIBLE_IMAGE="$(CRUCIBLE_IMAGE)") \
	  python3 tools/smoke/compose_smoke.py

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
