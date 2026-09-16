# Reproducible build: pinned base digest, pinned uv, locked dependencies.
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
COPY --from=uv /uv /usr/local/bin/uv
# .git is not in the build context, so hatch-vcs cannot read the tag itself.
# The caller passes it in: `docker build --build-arg VERSION=$(git describe ...)`.
# Unset, the package falls back to a dev version rather than failing the build.
ARG VERSION=0.0.0.dev0
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never SOURCE_DATE_EPOCH=0 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY crucible ./crucible
# No cache mount here, and --no-cache: uv keys its built-wheel cache on the
# source tree and not on SETUPTOOLS_SCM_PRETEND_VERSION, so a warm cache would
# reinstall a wheel built for a previous VERSION and the package version would
# silently disagree with the image label. Dependencies are already installed by
# the sync above, so this step only builds the project and costs nothing.
RUN uv sync --frozen --no-dev --no-cache

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG VERSION=0.0.0.dev0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="crucible" \
      org.opencontainers.image.description="A deterministic supervisor for AI coding workers." \
      org.opencontainers.image.source="https://github.com/sentania-labs/crucible" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
RUN groupadd --gid 1000 crucible && useradd --uid 1000 --gid 1000 --create-home crucible \
    && mkdir -p /var/lib/crucible/artifacts && chown -R crucible:crucible /var/lib/crucible
WORKDIR /app
COPY --from=build --chown=crucible:crucible /app /app
USER crucible
EXPOSE 8080
CMD ["crucible", "serve", "--all"]
