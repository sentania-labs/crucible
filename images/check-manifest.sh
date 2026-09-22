#!/usr/bin/env bash
# Verify that every declared worker-image tag, and the harness versions each image
# declares, match images/build.sh without building an image. build.sh remains the
# one definition of the build-input hash and of the harness labels.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
manifest="$here/manifest.env"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

images=()
for dir in "$here"/*; do
    [ -f "$dir/Dockerfile" ] || continue
    images+=("${dir##*/}")
done
[ ${#images[@]} -gt 0 ] || {
    echo "check-manifest.sh: no image directories found under $here" >&2
    exit 2
}

# build.sh owns the hash and tag calculation. Give it a Docker executable that
# accepts only the calls made after that calculation and writes an empty OCI
# manifest for the bookkeeping code to inspect. No daemon is contacted and no
# Dockerfile instruction is run.
mkdir -p "$tmp/bin" "$tmp/out"
cat > "$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}:${2:-}" in
    buildx:inspect)
        exit 1
        ;;
    buildx:create)
        exit 0
        ;;
    buildx:--builder)
        oci_dest=""
        for arg in "$@"; do
            case "$arg" in
                type=oci,*dest=*) oci_dest=${arg##*dest=} ;;
            esac
        done
        [ -n "$oci_dest" ] || {
            echo "manifest check Docker shim: build had no OCI destination" >&2
            exit 2
        }
        scratch=$(mktemp -d)
        trap 'rm -rf "$scratch"' EXIT
        printf '%s\n' '{"manifests":[{"digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}]}' \
            > "$scratch/index.json"
        tar -cf "$oci_dest" -C "$scratch" index.json
        ;;
    load:-q)
        exit 0
        ;;
    image:inspect)
        case "$*" in
            *'{{.Id}}'*) printf 'sha256:%064d\n' 0 ;;
            *'{{.Size}}'*) printf '0\n' ;;
            *) echo "manifest check Docker shim: unexpected inspect: $*" >&2; exit 2 ;;
        esac
        ;;
    *)
        echo "manifest check Docker shim: unexpected call: $*" >&2
        exit 2
        ;;
esac
EOF
chmod +x "$tmp/bin/docker"

PATH="$tmp/bin:$PATH" MANIFEST="$tmp/expected.env" OUT="$tmp/out" \
    "$here/build.sh" "${images[@]}" >/dev/null

failed=0
declare -A expected_keys=()
for image in "${images[@]}"; do
    key=$(printf '%s' "$image" | tr 'a-z-' 'A-Z_')
    expected_keys["$key"]=1
    expected=$(sed -n "s/^${key}=//p" "$tmp/expected.env")
    expected_carried=$(sed -n "s/^${key}_HARNESSES=//p" "$tmp/expected.env")
    count=$(grep -c "^${key}=" "$manifest" 2>/dev/null || true)
    actual=$(sed -n "s/^${key}=//p" "$manifest" 2>/dev/null || true)
    carried_count=$(grep -c "^${key}_HARNESSES=" "$manifest" 2>/dev/null || true)
    actual_carried=$(sed -n "s/^${key}_HARNESSES=//p" "$manifest" 2>/dev/null || true)
    digest_count=$(grep -c "^${key}_DIGEST=" "$manifest" 2>/dev/null || true)
    valid_digest_count=$(
        grep -c "^${key}_DIGEST=sha256:[0-9a-f]\{64\}$" "$manifest" 2>/dev/null || true
    )

    if [ "$count" -ne 1 ]; then
        echo "image manifest drift: $image has $count tag entries; run 'images/build.sh $image'" >&2
        failed=1
    elif [ "$actual" != "$expected" ]; then
        echo "image manifest drift: $image declares '$actual', expected '$expected'; run 'images/build.sh $image'" >&2
        failed=1
    elif [ "$carried_count" -ne 1 ] || [ "$actual_carried" != "$expected_carried" ]; then
        echo "image manifest drift: $image declares harnesses '$actual_carried', expected '$expected_carried'; run 'images/build.sh $image'" >&2
        failed=1
    elif [ "$digest_count" -ne 1 ] || [ "$valid_digest_count" -ne 1 ]; then
        echo "image manifest drift: $image has no single valid digest entry; run 'images/build.sh $image'" >&2
        failed=1
    fi
done

if [ -f "$manifest" ]; then
    declare -A orphaned_keys=()
    while IFS='=' read -r manifest_key _; do
        case "$manifest_key" in ""|'#'*) continue ;; esac
        base_key=${manifest_key%_DIGEST}
        base_key=${base_key%_HARNESSES}
        if [ -z "${expected_keys[$base_key]+present}" ] && [ -z "${orphaned_keys[$base_key]+seen}" ]; then
            echo "image manifest drift: manifest key $base_key has no image directory; remove its entries from images/manifest.env" >&2
            orphaned_keys["$base_key"]=1
            failed=1
        fi
    done < "$manifest"
fi

[ "$failed" -eq 0 ] || exit 1
printf 'image manifest: %d image tags match build.sh\n' "${#images[@]}"
