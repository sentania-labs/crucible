# Crucible worker base image: AGY, the Antigravity CLI (spec 13, ADR 0011).
# Debian slim by digest, uid 1000 worker, git, curl, jq, the CLI, nothing else.
# Shared pins mirror images/pins.env; build with images/build.sh.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

# Fetch stage: AGY is not on any package registry. Its installer
# (https://antigravity.google/cli/install.sh) reads a per-platform manifest
# and downloads a versioned tarball from a public GCS bucket; the URL below
# is the one that manifest named for 1.2.4 and it carries a build number, so
# it stays addressable after a newer release. HARNESS_SHA256 is the sha256 of
# that tarball. The manifest publishes a sha512 of the same file, checked
# when the pin was taken on 2026-09-16:
#   5811d39ec1bf96a82ed06de6b8ee2bb7f5be8d74423b8c52b6b975e8f0e2c84c
#   6cc2fa0baf902aad942c7566509c3ba6ddb5ef076260c6a635c4616e6ae17897
FROM ${BASE_IMAGE} AS fetch
ARG SOURCE_DATE_EPOCH
ARG HARNESS_VERSION=1.2.4
ARG HARNESS_BUILD=6085322963025920
ARG HARNESS_SHA256=dcd3e4d8c8afb1902d59c1ae52812458d2ddab67a5d2db44810c512910d918fe
ADD --checksum=sha256:${HARNESS_SHA256} --chmod=0644 \
    https://storage.googleapis.com/antigravity-public/antigravity-cli/${HARNESS_VERSION}-${HARNESS_BUILD}/linux-x64/cli_linux_x64.tar.gz \
    /fetch/agy.tar.gz
# The extracted file keeps the tarball entry mtime; pin it to the epoch so a
# repacked upstream tarball with the same bytes cannot change the layer.
RUN set -eu; \
    tar -xzf /fetch/agy.tar.gz -C /fetch antigravity; \
    mv /fetch/antigravity /fetch/agy; \
    chmod 0755 /fetch/agy; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/agy

FROM ${BASE_IMAGE}
# Declared here so it reaches the RUN below: useradd stamps /etc/shadow with
# the build day unless SOURCE_DATE_EPOCH is in its environment (S7).
ARG SOURCE_DATE_EPOCH
ARG DEBIAN_SNAPSHOT=20260916T000000Z
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG JQ_VERSION=1.6-2.1+deb12u2
ARG CA_CERTIFICATES_VERSION=20250419~deb12u1
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
        "ca-certificates=${CA_CERTIFICATES_VERSION}"; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/log/apt /var/log/dpkg.log /var/log/alternatives.log; \
    rm -f /var/cache/ldconfig/aux-cache; \
    groupadd --gid 1000 worker; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/worker --shell /bin/bash worker; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} +
# The snapshot sources stay in place on purpose: a per-project image built
# FROM this one that runs a plain apt-get update gets the same frozen
# package set. Check-Valid-Until: no lives in the stanzas, not on the
# command line, so that keeps working after the snapshot's Release
# metadata expires. Note that snapshot.debian.org is rate limited.
# Setuid and setgid bits are stripped from every binary (su, mount, passwd,
# ...) so a per-project image inherits none; the launch flags
# (--cap-drop ALL, no-new-privileges) are then belt and braces, not the
# only thing standing.
COPY --from=fetch /fetch/agy /usr/local/bin/agy

# Auto-update opt-out. AGY_CLI_DISABLE_AUTO_UPDATE is the variable the 1.2.4
# binary checks (found by inspecting the binary; the public docs do not list
# it). The read-only root makes "agy update" fail regardless (S7, S11).
ENV HOME=/home/worker \
    AGY_CLI_DISABLE_AUTO_UPDATE=1
USER 1000:1000
WORKDIR /home/worker
