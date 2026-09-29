#!/usr/bin/env bash
# The unit tier inside the worker image, the way a Kubernetes worker Pod runs it
# (hades #184, FDY-0134): uid 1000, a read-only root, no capabilities, no Docker
# socket, and fsGroup-style storage. A Pod's fsGroup makes every volume group 1000
# with the setgid bit, so a directory a test makes under /tmp (pytest's tmp_path)
# inherits setgid; a host run never sees that. This runs the same `make test-unit` a
# policy's verification runs, in that arrangement, so a test that only passes on a
# plain host fails here first.
#
#   tools/images/unit_in_image.sh [image]
#
# The image defaults to WORKER in images/manifest.env, which `make images` and `make
# images-check` leave in the daemon. The checkout is streamed in as a tar of the
# working tree (tracked and untracked files, minus what .gitignore excludes), so the
# daemon never needs to read this directory and uncommitted edits are what is tested.
#
# Two containers share one volume that stands in for the workspace claim:
#   1. `uv sync --frozen` with the network, the only step that needs it.
#   2. `make test-unit` with no network at all (UV_OFFLINE=1).
#
# Environment: DOCKER (the Docker CLI or the rootless wrapper).
set -euo pipefail

docker=${DOCKER:-docker}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
image=${1:-}
if [ -z "$image" ]; then
    image=$(sed -n 's/^WORKER=//p' "$repo_root/images/manifest.env")
fi
[ -n "$image" ] || { echo "unit_in_image.sh: no WORKER in images/manifest.env" >&2; exit 2; }

volume="crucible-unit-in-image-$$"
cleanup() { $docker volume rm -f "$volume" >/dev/null 2>&1 || true; }
trap cleanup EXIT
$docker volume create "$volume" >/dev/null

# What the Pod's securityContext and emptyDir volumes are (crucible/adapters/execution/
# k8sspec.py, pod_spec and memory_volume): an fsGroup emptyDir is root:1000, mode 2777.
pod=(
    --rm --user 1000:1000 --read-only --cap-drop ALL
    --security-opt no-new-privileges
    --tmpfs "/tmp:rw,exec,size=2g,uid=0,gid=1000,mode=2777"
    --tmpfs "/home/worker:rw,exec,size=2g,uid=0,gid=1000,mode=2777"
    --mount "type=volume,src=$volume,dst=/work,volume-nocopy"
)
# Docker makes a missing working directory as root, so the checkout's own directory
# is only named once the worker uid has made it.
in_repo=(-w /work/repo)

# The claim as the kubelet hands it over: group 1000, setgid. Only this step is root,
# as the kubelet is.
$docker run --rm --user 0:0 -v "$volume:/work" --entrypoint sh "$image" \
    -c 'chown 0:1000 /work && chmod 2770 /work'

# The checkout, made by the worker uid inside the setgid claim, as the preparer does.
git -C "$repo_root" ls-files -z --cached --others --exclude-standard \
    | tar -C "$repo_root" --null --ignore-failed-read -T - -cf - \
    | $docker run -i "${pod[@]}" -w /work --entrypoint sh "$image" \
        -c 'mkdir repo && tar -C repo --no-same-permissions -xf -'

$docker run "${pod[@]}" "${in_repo[@]}" --entrypoint uv "$image" sync --frozen --quiet

if ! $docker run "${pod[@]}" "${in_repo[@]}" --network none -e UV_OFFLINE=1 --entrypoint make "$image" \
    test-unit; then
    echo "unit tier fails inside the worker image ($image)" >&2
    exit 1
fi
