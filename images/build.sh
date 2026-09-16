#!/usr/bin/env bash
# Build the Crucible worker base images reproducibly (spec 13, ADR 0011).
#
#   images/build.sh [claude_code|codex|agy ...]     default: all three
#
# Environment:
#   OUT=<dir>       where the OCI and docker tarballs go (default images/out)
#   NO_CACHE=1      build from scratch, which is what a reproducibility check wants
#                   (any value other than empty or 0)
#   BUILDER=<name>  buildx docker-container builder to use or create (default crucible-images)
#
# Each image is tagged crucible-worker:<harness>-<version>-<build>, where
# <build> is the first 12 hex digits of the crucible.build_inputs hash: the
# sha256 over pins.env, the harness Dockerfile, and this script. Same inputs,
# same tag, and (S7) same digest.
#
# Never pushes. Prints one line per image: tag, OCI manifest digest, docker
# image ID, size in bytes.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$here/out}
builder=${BUILDER:-crucible-images}
harnesses=("$@")
[ ${#harnesses[@]} -gt 0 ] || harnesses=(claude_code codex agy)
for harness in "${harnesses[@]}"; do
    [ -f "$here/$harness/Dockerfile" ] || { echo "build.sh: no Dockerfile for harness '$harness'" >&2; exit 2; }
done
no_cache=""
case "${NO_CACHE:-0}" in 0|"") ;; *) no_cache="--no-cache" ;; esac

# shellcheck disable=SC1091
. "$here/pins.env"
: "${BASE_IMAGE:?}" "${DEBIAN_SNAPSHOT:?}" "${GIT_VERSION:?}" "${CURL_VERSION:?}" \
  "${JQ_VERSION:?}" "${CA_CERTIFICATES_VERSION:?}" "${SOURCE_DATE_EPOCH:?}" "${BUILDKIT_IMAGE:?}"

# The docker driver inside dockerd cannot write OCI output, and the OCI
# manifest digest is the one a registry would report, so builds run on a
# docker-container builder with a pinned BuildKit.
# An existing builder must run the pinned BuildKit, since the pin is part of
# the build inputs the tag is named after. A stale one is refused, not reused.
if info=$(docker buildx inspect "$builder" 2>/dev/null); then
    if ! grep -q -F "$BUILDKIT_IMAGE" <<<"$info"; then
        echo "build.sh: builder '$builder' exists but does not run $BUILDKIT_IMAGE; remove it (docker buildx rm $builder) or set BUILDER" >&2
        exit 2
    fi
else
    docker buildx create --name "$builder" --driver docker-container \
        --driver-opt "image=$BUILDKIT_IMAGE" --bootstrap >/dev/null
fi

mkdir -p "$out"
created=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)

for harness in "${harnesses[@]}"; do
    dir="$here/$harness"
    dockerfile="$dir/Dockerfile"

    version=$(sed -n 's/^ARG HARNESS_VERSION=//p' "$dockerfile" | head -n1)
    [ -n "$version" ] || { echo "build.sh: $dockerfile has no ARG HARNESS_VERSION" >&2; exit 2; }

    inputs=$(cat "$here/pins.env" "$dockerfile" "$here/build.sh" | sha256sum | cut -c1-64)
    build=${inputs:0:12}
    tag="crucible-worker:$harness-$version-$build"
    stem="$out/crucible-worker-$harness-$version-$build"

    # shellcheck disable=SC2086
    docker buildx --builder "$builder" build $no_cache --platform linux/amd64 \
        --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
        --build-arg "BASE_IMAGE=$BASE_IMAGE" \
        --build-arg "DEBIAN_SNAPSHOT=$DEBIAN_SNAPSHOT" \
        --build-arg "GIT_VERSION=$GIT_VERSION" \
        --build-arg "CURL_VERSION=$CURL_VERSION" \
        --build-arg "JQ_VERSION=$JQ_VERSION" \
        --build-arg "CA_CERTIFICATES_VERSION=$CA_CERTIFICATES_VERSION" \
        --label "org.opencontainers.image.version=$harness-$version-$build" \
        --label "org.opencontainers.image.created=$created" \
        --label "crucible.harness=$harness" \
        --label "crucible.harness_version=$version" \
        --label "crucible.build_inputs=sha256:$inputs" \
        --provenance=false --sbom=false \
        --output "type=oci,rewrite-timestamp=true,dest=$stem.oci.tar" \
        --output "type=docker,rewrite-timestamp=true,dest=$stem.docker.tar" \
        -t "$tag" "$dir"

    digest=$(tar -xOf "$stem.oci.tar" index.json | jq -r '.manifests[0].digest')
    docker load -q -i "$stem.docker.tar" >/dev/null
    id=$(docker image inspect -f '{{.Id}}' "$tag")
    size=$(docker image inspect -f '{{.Size}}' "$tag")
    printf '%s digest=%s id=%s size=%s\n' "$tag" "$digest" "$id" "$size"
done
