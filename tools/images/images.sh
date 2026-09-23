#!/usr/bin/env bash
# The worker images, built from a staged copy of images/ (13, C11, FDY-0072).
#
#   tools/images/images.sh build     build every image and write images/manifest.env back
#   tools/images/images.sh check     build every image; fail when any tag, harness list
#                                    or OCI digest differs from images/manifest.env
#   tools/images/images.sh publish   check, then push each image's OCI archive to
#                                    WORKER_REGISTRY:<tag>, never over a different
#                                    digest, and verify every published digest
#
# Why a staged copy: on the reference workstation the build daemon is the `crucible`
# service user's rootless daemon, and that user cannot read a checkout under the
# operator's home (mode 750). The Docker CLI the DOCKER wrapper runs as that user
# reads the build context and writes the OCI and docker archives, so both live in a
# scratch directory it can reach. images/build.sh still does all the building; this
# script only moves its inputs and its manifest. CI runs the same script on a runner
# whose daemon can read anything, where the staging is merely harmless.
#
# Environment: DOCKER (the Docker CLI or the rootless wrapper), NO_CACHE and CACHE_DIR
# (passed to build.sh), IMAGES_STAGE_ROOT (where the scratch directory goes, default
# TMPDIR or /tmp), and for publish WORKER_REGISTRY plus, when the registry wants them,
# REGISTRY_USERNAME and REGISTRY_PASSWORD. No credential is ever an argument.
# WORKER_TAG_PREFIX (publish only, default empty) publishes <prefix><tag> instead of
# <tag>: CI's throwaway ci-<sha>- tags (FDY-0090). The release never sets it.
# WORKER_RELEASE_VERSION (publish only, default empty) publishes the Crucible release
# version and moves latest instead of the fingerprint tag (13, 24): the release sets
# it, from the git tag, exactly as the service image build does; CI's ghcr-publish job
# never sets it. Mutually exclusive with WORKER_TAG_PREFIX.
set -euo pipefail

mode=${1:-}
case "$mode" in
    build|check|publish) ;;
    *) echo "images.sh: usage: images.sh build|check|publish" >&2; exit 2 ;;
esac

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_dir="$repo_root/images"

stage=$(mktemp -d "${IMAGES_STAGE_ROOT:-${TMPDIR:-/tmp}}/crucible-images.XXXXXX")
trap 'rm -rf "$stage"' EXIT
# The build inputs are public, so the staged tree is world-readable; only `out`, where
# the daemon user's CLI writes the archives, is writable by it, and it is sticky so no
# other local user can replace an archive once written. build.sh then checks the
# image it loads against the OCI archive. Previous archives in images/out are never
# staged.
tar -C "$source_dir" --exclude=./out -cf - . | tar -C "$stage" -xf -
mkdir "$stage/out"
chmod -R a+rX "$stage"
chmod 1777 "$stage/out"

# Every image is built, so the manifest is written fresh: an entry for an image
# directory that no longer exists cannot survive a build.
rm -f "$stage/manifest.env"
OUT="$stage/out" MANIFEST="$stage/manifest.env" "$stage/build.sh"

case "$mode" in
    build)
        cp "$stage/manifest.env" "$source_dir/manifest.env"
        echo "images.sh: wrote images/manifest.env"
        ;;
    check|publish)
        # The whole file, not a subset: a tag, a harness version or a digest that the
        # build did not reproduce is drift, and so is an entry the build did not write.
        if ! diff -u "$source_dir/manifest.env" "$stage/manifest.env"; then
            echo "images.sh: the build from the pinned inputs does not reproduce images/manifest.env" >&2
            exit 1
        fi
        echo "images.sh: every image reproduced its declared tag, harness versions and digest"
        ;;
esac

if [ "$mode" = publish ]; then
    : "${WORKER_REGISTRY:?set WORKER_REGISTRY, for example ghcr.io/sentania-labs/crucible-worker}"
    python3 "$repo_root/tools/release/worker_images.py" publish \
        --manifest "$source_dir/manifest.env" --archives "$stage/out" \
        --repository "$WORKER_REGISTRY" --tag-prefix "${WORKER_TAG_PREFIX:-}" \
        --release-version "${WORKER_RELEASE_VERSION:-}"
    python3 "$repo_root/tools/release/worker_images.py" verify \
        --manifest "$source_dir/manifest.env" --repository "$WORKER_REGISTRY" \
        --tag-prefix "${WORKER_TAG_PREFIX:-}" --release-version "${WORKER_RELEASE_VERSION:-}"
fi
