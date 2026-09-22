# Crucible worker image for Hermes 0.19.0 on a credential-free local endpoint.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

FROM ${BASE_IMAGE} AS fetch
ARG HARNESS_VERSION=0.19.0
ARG HARNESS_SHA256=bd0bac012aee38a60894781f4597dc29ee7bedb3448540249921f10d3bef327f
ADD --checksum=sha256:${HARNESS_SHA256} --chmod=0644 \
    https://files.pythonhosted.org/packages/e5/30/c85be8290e9565dc3c7a9720e93f3e59e09b1b163487be4946c3aa848f80/hermes_agent-0.19.0-py3-none-any.whl \
    /fetch/hermes_agent-0.19.0-py3-none-any.whl

FROM ${BASE_IMAGE}
ARG SOURCE_DATE_EPOCH
ARG DEBIAN_SNAPSHOT=20260916T000000Z
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG JQ_VERSION=1.6-2.1+deb12u2
ARG CA_CERTIFICATES_VERSION=20250419~deb12u1
ARG LAB_CA_SHA256=567f110bb80bb2179b44995c78c7a49a4149085a59f6acb73cf37c6503a0481a
ARG PYTHON3_VERSION=3.11.2-1+b1
ARG PYTHON3_VENV_VERSION=3.11.2-1+b1
RUN set -eu; \
    printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "git=${GIT_VERSION}" \
        "curl=${CURL_VERSION}" \
        "jq=${JQ_VERSION}" \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "python3=${PYTHON3_VERSION}" \
        "python3-venv=${PYTHON3_VENV_VERSION}"; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/log/apt /var/log/dpkg.log /var/log/alternatives.log; \
    rm -f /var/cache/ldconfig/aux-cache; \
    groupadd --gid 1000 worker; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/worker --shell /bin/bash worker; \
    python3 -m venv /opt/hermes; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} +
COPY ca/sentania-lab-root.crt /usr/local/share/ca-certificates/sentania-lab-root.crt
RUN set -eu; \
    echo "${LAB_CA_SHA256}  /usr/local/share/ca-certificates/sentania-lab-root.crt" | sha256sum -c -; \
    update-ca-certificates
COPY hermes/requirements.lock /tmp/requirements.lock
COPY --from=fetch /fetch/hermes_agent-0.19.0-py3-none-any.whl /fetch/hermes_agent-0.19.0-py3-none-any.whl
RUN set -eu; \
    /opt/hermes/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock; \
    /opt/hermes/bin/pip install --no-cache-dir --no-deps --force-reinstall /fetch/hermes_agent-0.19.0-py3-none-any.whl; \
    test "$(/opt/hermes/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("hermes-agent"))')" = "0.19.0"; \
    rm -rf /root/.cache /tmp/requirements.lock /fetch
COPY --chmod=0755 hermes/crucible-hermes.py /usr/local/bin/crucible-hermes
# Hermes 0.19.0 has no automatic update facility or opt-out setting. The exact wheel
# is pinned above and the provider launches the image with a read-only root filesystem.
ENV HOME=/home/worker \
    PATH=/opt/hermes/bin:/usr/local/bin:/usr/bin:/bin \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
USER 1000:1000
WORKDIR /home/worker
