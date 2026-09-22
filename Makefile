# Same targets locally and in CI (18). One definition, two callers.
#
# Crucible runs on the dedicated rootless daemon of the `crucible` service user
# (13, ADR 0004, S9). Point DOCKER at that daemon before running anything that
# creates containers. On the reference workstation the service user has no login
# shell, so the operator's wrapper is:
#
#   make e2e DOCKER='sudo -u crucible -H env HOME=/var/lib/crucible-docker \
#     XDG_RUNTIME_DIR=/run/user/999 DOCKER_HOST=unix:///run/user/999/docker.sock docker'
#
# CI has an ordinary rootful daemon and needs none of that: DOCKER stays `docker`.
DOCKER ?= docker
COMPOSE ?= $(DOCKER) compose
# Normal local and CI boots build from the working tree. Release passes
# `--pull never` here after it has built and classified the candidate image.
COMPOSE_UP_FLAGS ?= --build
UV ?= uv

# The rootless daemon's socket, derived, never hardcoded: the uid differs per host (S9).
CRUCIBLE_UID := $(shell id -u crucible 2>/dev/null)
CRUCIBLE_DOCKER_SOCKET ?= $(if $(CRUCIBLE_UID),/run/user/$(CRUCIBLE_UID)/docker.sock,/var/run/docker.sock)
export CRUCIBLE_DOCKER_SOCKET

# The hostnames the egress proxy permits: the union of the policy's egress_allowlist
# and the adapters' declared model endpoints (13, S6). `proxy-config` writes the squid
# configuration from this list and Crucible refuses to launch an attempt that needs a
# host the running proxy does not permit.
EGRESS_ALLOWLIST ?= github.com objects.githubusercontent.com pypi.org files.pythonhosted.org registry.npmjs.org api.anthropic.com api.openai.com auth.openai.com chatgpt.com daily-cloudcode-pa.googleapis.com oauth2.googleapis.com www.googleapis.com lh3.googleusercontent.com
ROUTING_POLICY_FILES ?=
WORKERS_SUBNET ?= 10.88.0.0/24

# The publisher's own allowlist (23 step 3). It is deliberately not the workers' list:
# the one container that holds a GitHub credential reaches GitHub and nothing else.
PUBLISH_ALLOWLIST ?= github.com api.githubusercontent.com api.github.com
PUBLISH_SUBNET ?= 10.88.1.0/24

# Where `make deploy-local` runs a published release: a directory the `crucible`
# service user owns, because that user cannot read the operator's home (750) and so
# cannot run the normal-mode stack from a working tree under it. DEPLOY_TAG is an exact
# version, never `latest`: a deployment pins, and that is what makes a rollback one word.
# It is a deployment pin, not the package version, which still comes from the git tag
# (release.md). It does not follow a new release on its own: bump it, or pass DEPLOY_TAG.
CRUCIBLE_SERVICE_USER ?= crucible
CRUCIBLE_DEPLOY_DIR ?= /var/lib/crucible/deploy
CRUCIBLE_CREDENTIAL_ROOT ?= /var/lib/crucible/credentials
DEPLOY_TAG ?= 0.2.1
CRUCIBLE_DEPLOY_IMAGE ?= ghcr.io/sentania-labs/crucible:$(DEPLOY_TAG)
CRUCIBLE_DEPLOY_PORT ?= 8080

.PHONY: up dev down reset lint check-image-manifest scan scan-tree scan-history smoke test test-unit \
	test-integration e2e e2e-github e2e-live e2e-admin e2e-image build proxy-config proxies preflight \
	e2e-kind manifests deploy-kind release-images-classify release-images-pull release-images-verify \
	deploy-local deploy-local-down

up: preflight proxy-config ## normal mode: postgres, proxies, migrate, crucible
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d $(COMPOSE_UP_FLAGS) --wait

dev: preflight proxy-config ## developer mode: postgres and the two proxies; run `uv run crucible serve --all` on the host
	@test -f .env || cp .env.example .env
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml --profile dev up -d --wait

preflight: ## S9 follow-up 1: say what is wrong rather than letting compose be cryptic
	@test -n "$(CRUCIBLE_UID)" \
	  || echo "preflight: no 'crucible' service user, so this is ADR 0004's fallback: the host daemon behind the proxy. A compromise of Crucible is then a compromise of the host (13)."
	@test -S "$(CRUCIBLE_DOCKER_SOCKET)" \
	  || { command -v sudo >/dev/null && sudo -n test -S "$(CRUCIBLE_DOCKER_SOCKET)"; } \
	  || { echo "preflight: no docker socket at $(CRUCIBLE_DOCKER_SOCKET)"; exit 2; }
	@test -f /etc/apparmor.d/rootlesskit || echo "preflight: warning, /etc/apparmor.d/rootlesskit is missing; the rootless daemon will not start after a reboot on Ubuntu 24.04 (S9)"
	@$(DOCKER) info --format '{{range .SecurityOptions}}{{.}} {{end}}' 2>/dev/null | grep -q rootless \
	  || echo "preflight: warning, DOCKER is not pointed at a rootless daemon"
	@$(DOCKER) info --format '{{.CgroupVersion}}' 2>/dev/null | grep -q '^2$$' \
	  || echo "preflight: warning, cgroup v2 is what the memory and pids limits need (05b, S9)"

proxy-config: ## write the egress proxy's allowlist from EGRESS_ALLOWLIST (13, S6)
	@mkdir -p var/egress
	@python3 -m crucible.application.proxy_config \
	  --output var/egress/squid.conf --subnet "$(WORKERS_SUBNET)" \
	  $(foreach host,$(EGRESS_ALLOWLIST),--host $(host)) \
	  $(if $(CRUCIBLE_SPARK_ENDPOINT_URL),--configured-local-endpoint "$(CRUCIBLE_SPARK_ENDPOINT_URL)") \
	  $(foreach policy,$(ROUTING_POLICY_FILES),--routing-policy "$(policy)")
	@{ \
	  echo "# Generated by 'make proxy-config'. The publisher's allowlist (23 step 3):"; \
	  echo "# the one container that holds a GitHub credential reaches GitHub and nothing else."; \
	  echo "acl publishers src $(PUBLISH_SUBNET)"; \
	  echo "acl SSL_ports port 443"; \
	  echo "acl Safe_ports port 443"; \
	  echo "acl CONNECT method CONNECT"; \
	  for host in $(PUBLISH_ALLOWLIST); do echo "acl allowed dstdomain $$host"; done; \
	  echo "http_access deny !Safe_ports"; \
	  echo "http_access deny CONNECT !SSL_ports"; \
	  echo "http_access allow publishers CONNECT allowed"; \
	  echo "http_access deny all"; \
	  echo "http_port 3128"; \
	  echo "cache deny all"; \
	  echo "access_log /var/log/squid/access.log"; \
	  echo "pid_filename none"; \
	  echo "shutdown_lifetime 1 second"; \
	} > var/egress/squid-publish.conf
	@echo "wrote var/egress/squid.conf from remote hosts and enabled local routing entries"
	@echo "wrote var/egress/squid-publish.conf with $(words $(PUBLISH_ALLOWLIST)) allowed hostname(s)"

proxies: preflight proxy-config ## bring up only the socket proxy and the egress proxy
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d docker-socket-proxy egress-proxy

down:
	$(COMPOSE) --profile "*" down

reset: ## DESTRUCTIVE: down plus postgres, artifact, and credential volumes
	$(COMPOSE) --profile "*" down --volumes

lint: check-image-manifest
	$(UV) sync --frozen --quiet
	$(UV) run ruff format --check crucible tests tools/release tools/smoke
	$(UV) run ruff check crucible tests tools/release tools/smoke
	$(UV) run mypy crucible tests tools/release tools/smoke
	$(UV) run lint-imports

check-image-manifest: ## fail when a declared worker-image tag is stale
	images/check-manifest.sh

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

release-images-classify: ## classify release candidate and supporting images without pulling
	@CRUCIBLE_IMAGE="$${CRUCIBLE_IMAGE:-ghcr.io/sentania-labs/crucible:classification-test}" \
	  POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-classification-only}" COMPOSE="$(COMPOSE)" \
	  DOCKER="$(DOCKER)" python3 tools/release/compose_images.py classify

release-images-pull: ## pull only images that do not resolve to CRUCIBLE_IMAGE
	@test -n "$(CRUCIBLE_IMAGE)" || { echo "set CRUCIBLE_IMAGE"; exit 2; }
	@COMPOSE="$(COMPOSE)" DOCKER="$(DOCKER)" python3 tools/release/compose_images.py pull-supporting

release-images-verify: ## prove every CRUCIBLE_IMAGE service container uses the local candidate
	@test -n "$(CRUCIBLE_IMAGE)" || { echo "set CRUCIBLE_IMAGE"; exit 2; }
	@COMPOSE="$(COMPOSE)" DOCKER="$(DOCKER)" python3 tools/release/compose_images.py verify-candidate

test: test-unit test-integration

test-unit:
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/unit -q

test-integration: ## needs Docker for postgres:16 (testcontainers) or CRUCIBLE_TEST_DATABASE_URL
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/integration -q -m integration

e2e-image: ## build the e2e worker image (18) on whichever daemon DOCKER names
	DOCKER_HOST=$${DOCKER_HOST:-} images/build.sh script-harness

e2e: check-image-manifest ## the Docker-provider end-to-end tier (18): real containers, no model
	$(UV) sync --frozen --quiet
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	$(UV) run pytest tests/e2e -q -m e2e

e2e-kind: check-image-manifest ## Kubernetes-provider e2e on a disposable kind cluster (18, 26)
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	tools/kind/e2e-kind.sh

manifests: ## render deploy/kubernetes and validate every object; needs kubectl and kubeconform
	UV="$(UV)" tools/manifests/validate.sh

# Bring the deployment manifests up on a disposable kind cluster and run one task through
# the deployed API on the Kubernetes provider (C9, sdlc skill step 3). This is the
# author's half of "done means seen working": it proves the manifests and the image, and
# it deliberately proves nothing cluster-specific (github-ci skill).
#
# DEPLOY_KIND_IMAGE is the Crucible image under test. It defaults to a locally built one
# because the published 0.3.3 predates the Kubernetes provider (docs/implementation-notes/c9.md);
# pass a published tag once one carries the provider.
DEPLOY_KIND_IMAGE ?=
deploy-kind: check-image-manifest ## deploy the manifests on a disposable kind cluster and run one task
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_DEPLOY_KIND_IMAGE="$(DEPLOY_KIND_IMAGE)" \
	UV="$(UV)" tools/kind/deploy-kind.sh

# The live GitHub tier (23). Local only, never in CI: it mints a real installation token
# from the mounted App key and opens a real pull request on one throwaway repository,
# then deletes every branch and closes every pull request it created. It never touches
# the default branch. The three variables below name files and a repository; no key,
# token, or secret is ever a value here.
#
#   make e2e-github \
#     CRUCIBLE_GITHUB_APP_JSON=~/path/to/app.json \
#     CRUCIBLE_GITHUB_APP_KEY=~/path/to/app.pem \
#     CRUCIBLE_GITHUB_TARGET_REPO=owner/throwaway
e2e-github: ## the live GitHub tier: a real App against a throwaway repository
	$(UV) sync --frozen --quiet
	@test -n "$(CRUCIBLE_GITHUB_APP_JSON)" || { echo "set CRUCIBLE_GITHUB_APP_JSON"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_APP_KEY)" || { echo "set CRUCIBLE_GITHUB_APP_KEY"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_TARGET_REPO)" || { echo "set CRUCIBLE_GITHUB_TARGET_REPO"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_github

# The live harness tier (07, 12, 18). Local only, never in CI: it runs the real Claude
# Code, Codex and AGY images with the dedicated Crucible credentials (never the
# operator's daily-use directories) on the rootless daemon, one harness at a time, and
# publishes a trivial change to the throwaway repository, then closes the pull request
# and deletes the branch. Every variable names a directory, a file, a repository or a
# harness; no key, token or secret is ever a value here.
#
#   make e2e-live HARNESS=claude_code \
#     CRUCIBLE_LIVE_CREDENTIAL_ROOT=/path/to/dedicated/credentials \
#     CRUCIBLE_GITHUB_APP_JSON=~/path/to/app.json \
#     CRUCIBLE_GITHUB_APP_KEY=~/path/to/app.pem \
#     CRUCIBLE_GITHUB_TARGET_REPO=owner/throwaway \
#     DOCKER='<the rootless daemon wrapper above>'
HARNESS ?= all
e2e-live: ## the live harness tier: real harness images, the dedicated credentials, a throwaway repository
	$(UV) sync --frozen --quiet
	@if [ "$(HARNESS)" != "hermes" ]; then test -n "$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" || { echo "set CRUCIBLE_LIVE_CREDENTIAL_ROOT"; exit 2; }; fi
	@if [ "$(HARNESS)" = "hermes" ] || [ "$(HARNESS)" = "all" ]; then test -n "$(CRUCIBLE_SPARK_ENDPOINT_URL)" || { echo "set CRUCIBLE_SPARK_ENDPOINT_URL"; exit 2; }; fi
	@test -n "$(CRUCIBLE_GITHUB_APP_JSON)" || { echo "set CRUCIBLE_GITHUB_APP_JSON"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_APP_KEY)" || { echo "set CRUCIBLE_GITHUB_APP_KEY"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_TARGET_REPO)" || { echo "set CRUCIBLE_GITHUB_TARGET_REPO"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_LIVE_CREDENTIAL_ROOT="$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" \
	CRUCIBLE_LIVE_HARNESSES="$(HARNESS)" \
	CRUCIBLE_LIVE_MODELS='$(CRUCIBLE_LIVE_MODELS)' \
	CRUCIBLE_LIVE_IMAGES='$(CRUCIBLE_LIVE_IMAGES)' \
	CRUCIBLE_LIVE_AGY_MOUNT_MODE="$(CRUCIBLE_LIVE_AGY_MOUNT_MODE)" \
	CRUCIBLE_LIVE_REPORT="$(CRUCIBLE_LIVE_REPORT)" \
	CRUCIBLE_SPARK_ENDPOINT_URL="$(CRUCIBLE_SPARK_ENDPOINT_URL)" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_live -s

# The live administration tier (25, C5b): every row of the operations table through
# `/v1/admin` and through `crucible-admin`, against a live stack on the daemon DOCKER
# names, and `credentials probe` with the dedicated credentials for each harness. Rotate
# and remove act on scratch copies inside the artifact root, never on the dedicated
# root. The GitHub variables are optional; without them `github check` is recorded as
# not configured. Again no key, token or secret is ever a value here.
#
#   make e2e-admin \
#     CRUCIBLE_LIVE_CREDENTIAL_ROOT=/path/to/dedicated/credentials \
#     DOCKER='<the rootless daemon wrapper above>'
e2e-admin: ## the live administration tier: API and CLI parity on a live stack, probes with the dedicated credentials
	$(UV) sync --frozen --quiet
	@test -n "$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" || { echo "set CRUCIBLE_LIVE_CREDENTIAL_ROOT"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_LIVE_CREDENTIAL_ROOT="$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" \
	CRUCIBLE_LIVE_REPORT="$(CRUCIBLE_LIVE_REPORT)" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_admin -s

build:
	docker build -t crucible:dev .

deploy-local: proxy-config ## run a pinned published release on the rootless daemon from /var/lib/crucible/deploy
	CRUCIBLE_SERVICE_USER="$(CRUCIBLE_SERVICE_USER)" \
	CRUCIBLE_DEPLOY_DIR="$(CRUCIBLE_DEPLOY_DIR)" \
	CRUCIBLE_CREDENTIAL_ROOT="$(CRUCIBLE_CREDENTIAL_ROOT)" \
	CRUCIBLE_DEPLOY_IMAGE="$(CRUCIBLE_DEPLOY_IMAGE)" \
	CRUCIBLE_DEPLOY_PORT="$(CRUCIBLE_DEPLOY_PORT)" \
	CRUCIBLE_SPARK_ENDPOINT_URL="$(CRUCIBLE_SPARK_ENDPOINT_URL)" \
	CRUCIBLE_EGRESS_ALLOWLIST_HOSTS="$(EGRESS_ALLOWLIST)" \
	CRUCIBLE_WORKERS_SUBNET_CIDR="$(WORKERS_SUBNET)" \
	tools/deploy/deploy_local.sh up

deploy-local-down: ## stop the deployed stack; the postgres and artifact volumes are kept
	CRUCIBLE_SERVICE_USER="$(CRUCIBLE_SERVICE_USER)" \
	CRUCIBLE_DEPLOY_DIR="$(CRUCIBLE_DEPLOY_DIR)" \
	tools/deploy/deploy_local.sh down
