#!/usr/bin/env bash
# Push the worker images the release just built with `docker push`, the way the service
# image and every ScarGuard service are pushed (13, 24; the operator's decision,
# 2026-09-23).
#
#   VERSION=0.5.3 WORKER_REGISTRY=ghcr.io/sentania-labs/crucible-worker \
#     tools/release/push_worker_images.sh
#
# `make images-check` has already built both images, loaded them into the daemon under
# the tags images/manifest.env names, and proved the build reproduces that file. This
# script tags them WORKER_REGISTRY:<VERSION> and WORKER_REGISTRY:script-harness-<VERSION>
# and pushes each one, never over an existing version tag:
#
#   - absent (the registry answers "not found", nothing else) is pushed;
#   - present with this build's crucible.build_inputs label is a re-run of a
#     half-published release and is left alone: the same inputs build the same image;
#   - present with any other label, or an answer that is neither, stops the release.
#
# latest and script-harness-latest are not touched here: the release's "move latest"
# step copies them on the registry from these version tags, under the same
# highest-version check as the service image, so latest always carries the version
# tag's own digest.
#
# Environment: VERSION, WORKER_REGISTRY, DOCKER (the Docker CLI or the rootless
# wrapper, default docker), MANIFEST (default images/manifest.env). Log in first; no
# credential is ever read here.
set -euo pipefail

: "${VERSION:?set VERSION, the release version without the leading v}"
: "${WORKER_REGISTRY:?set WORKER_REGISTRY, for example ghcr.io/sentania-labs/crucible-worker}"
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
manifest=${MANIFEST:-$repo_root/images/manifest.env}
read -r -a docker_cmd <<< "${DOCKER:-docker}"

declared() {
    local value
    value=$(sed -n "s/^$1=//p" "$manifest")
    [ -n "$value" ] || { echo "push_worker_images: $manifest declares no $1 image" >&2; exit 1; }
    printf '%s' "$value"
}

push_one() {
    local local_ref=$1 remote="$WORKER_REGISTRY:$2" inputs labels published err
    inputs=$("${docker_cmd[@]}" image inspect -f '{{index .Config.Labels "crucible.build_inputs"}}' "$local_ref")
    [ -n "$inputs" ] || { echo "push_worker_images: $local_ref carries no crucible.build_inputs label" >&2; exit 1; }
    err=$(mktemp)
    if labels=$("${docker_cmd[@]}" buildx imagetools inspect "$remote" \
        --format '{{json .Image.Config.Labels}}' 2>"$err"); then
        rm -f "$err"
        published=$(jq -r '."crucible.build_inputs" // empty' <<<"$labels")
        if [ "$published" != "$inputs" ]; then
            echo "::error::$remote is already published from build inputs ${published:-unknown}, not this build's $inputs. A published version is never overwritten. Cut a new version instead." >&2
            exit 1
        fi
        echo "push_worker_images: $remote is already published from these build inputs; leaving it alone"
    # Only buildx's own "not found" means absent; its last line, since a warning may
    # come first. A network error, a rate limit or a denied read is not absent.
    elif [ "$(tail -n 1 "$err")" = "ERROR: $remote: not found" ]; then
        rm -f "$err"
        "${docker_cmd[@]}" tag "$local_ref" "$remote"
        "${docker_cmd[@]}" push "$remote"
    else
        echo "::error::cannot tell whether $remote is already published, so refusing to push: $(cat "$err")" >&2
        rm -f "$err"
        exit 1
    fi
}

push_one "$(declared WORKER)" "$VERSION"
push_one "$(declared SCRIPT_HARNESS)" "script-harness-$VERSION"
