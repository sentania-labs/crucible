# Crucible worker base image: Codex CLI (spec 13, ADR 0011).
# Debian slim by digest, uid 1000 worker, git, curl, jq, the CLI, nothing else.
# Shared pins mirror images/pins.env; build with images/build.sh.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

# Fetch stage: the static musl release binary from the GitHub release
# rust-v<version>, verified against HARNESS_SHA256, the sha256 of the
# tarball as downloaded on 2026-09-16. Upstream publishes no plain checksum
# for this asset (codex-package_SHA256SUMS covers the npm tarballs only);
# its .sigstore file is a cosign-format signature that was not verified
# here (no cosign on the reference workstation). The extracted binary is
# byte-identical to the workstation's own Codex install of the same
# version. Nothing else from this stage lands in the final image.
FROM ${BASE_IMAGE} AS fetch
ARG SOURCE_DATE_EPOCH
ARG HARNESS_VERSION=0.153.4
ARG HARNESS_SHA256=f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30
ADD --checksum=sha256:${HARNESS_SHA256} --chmod=0644 \
    https://github.com/openai/codex/releases/download/rust-v${HARNESS_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz \
    /fetch/codex.tar.gz
# The extracted file keeps the tarball entry mtime; pin it to the epoch so a
# repacked upstream tarball with the same bytes cannot change the layer.
RUN set -eu; \
    tar -xzf /fetch/codex.tar.gz -C /fetch codex-x86_64-unknown-linux-musl; \
    mv /fetch/codex-x86_64-unknown-linux-musl /fetch/codex; \
    chmod 0755 /fetch/codex; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/codex

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
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
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
# FROM this one that runs apt-get gets the same frozen package set. Note
# that snapshot.debian.org is rate limited.
# Setuid and setgid bits are stripped from every binary (su, mount, passwd,
# ...) so a per-project image inherits none; the launch flags
# (--cap-drop ALL, no-new-privileges) are then belt and braces, not the
# only thing standing.
COPY --from=fetch /fetch/codex /usr/local/bin/codex

# Codex has no environment variable for its update check. The documented
# knob is the config key check_for_update_on_startup=false, which the adapter
# passes at launch (codex -c check_for_update_on_startup=false) because the
# config directory is the credential mount, not part of this image. The
# read-only root makes "codex update" fail regardless (S7, S11).
ENV HOME=/home/worker
USER 1000:1000
WORKDIR /home/worker
