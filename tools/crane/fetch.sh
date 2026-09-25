#!/usr/bin/env bash
# Print the path of the crane binary the service image ships, fetching it on first use.
#
# The version and the release archive's SHA-256 are read from the Dockerfile's
# CRANE_VERSION and CRANE_SHA256 build args, so the tests and CI run exactly the binary
# the image carries, and a pin changes in one place. The archive is checked before it is
# extracted and again on every call; a mismatch fails and leaves nothing behind.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
pin() { awk -F= -v name="$1" '$1 == "ARG " name { print $2; exit }' "$root/Dockerfile"; }
version=$(pin CRANE_VERSION)
sha256=$(pin CRANE_SHA256)
if [ -z "$version" ] || [ -z "$sha256" ]; then
  echo "crane: the Dockerfile pins no CRANE_VERSION or CRANE_SHA256" >&2
  exit 2
fi

cache="${XDG_CACHE_HOME:-$HOME/.cache}/crucible/crane-${version}"
archive="$cache/go-containerregistry_Linux_x86_64.tar.gz"
mkdir -p "$cache"
if ! echo "${sha256}  ${archive}" | sha256sum -c - >/dev/null 2>&1; then
  partial=$(mktemp "$cache/download.XXXXXX")
  trap 'rm -f "$partial"' EXIT
  curl -fsSL --retry 4 -o "$partial" \
    "https://github.com/google/go-containerregistry/releases/download/v${version}/go-containerregistry_Linux_x86_64.tar.gz"
  if ! echo "${sha256}  ${partial}" | sha256sum -c - >/dev/null; then
    echo "crane: the v${version} archive does not match the pinned SHA-256" >&2
    exit 1
  fi
  mv "$partial" "$archive"
fi
if [ ! -x "$cache/crane" ]; then
  # Extracted beside the cache and moved into place, so a concurrent caller never runs
  # a half-written binary.
  staging=$(mktemp -d "$cache/extract.XXXXXX")
  tar -xzf "$archive" -C "$staging" crane
  chmod 0755 "$staging/crane"
  mv "$staging/crane" "$cache/crane"
  rmdir "$staging"
fi
echo "$cache/crane"
