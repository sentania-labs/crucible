# Crucible worker base image: Claude Code (spec 13, ADR 0011).
# Debian slim by digest, uid 1000 worker, git, curl, jq, the CLI, nothing else.
# Shared pins mirror images/pins.env; build with images/build.sh.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

# Fetch stage: the release binary, verified against the checksum published in
# that release's manifest.json. Nothing from this stage but the binary lands
# in the final image.
FROM ${BASE_IMAGE} AS fetch
ARG SOURCE_DATE_EPOCH
ARG HARNESS_VERSION=2.1.273
ARG HARNESS_SHA256=6c752e2cc7c110c9df15f26d8d134d438c5ae95dbd610efc1a308bf7f9c5f6c1
ADD --checksum=sha256:${HARNESS_SHA256} --chmod=0755 \
    https://downloads.claude.ai/claude-code-releases/${HARNESS_VERSION}/linux-x64/claude \
    /fetch/claude
# ADD from a URL takes the file's mtime from the server's Last-Modified
# header, which rewrite-timestamp leaves alone when it is older than the
# epoch; a re-upload of the same bytes would change the layer. Pin it.
RUN touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/claude

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
COPY --from=fetch /fetch/claude /usr/local/bin/claude

# Auto-update opt-out, both documented in the Claude Code environment
# variable reference: DISABLE_AUTOUPDATER stops background updates,
# DISABLE_UPDATES also blocks a manual "claude update".
ENV HOME=/home/worker \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1
USER 1000:1000
WORKDIR /home/worker
