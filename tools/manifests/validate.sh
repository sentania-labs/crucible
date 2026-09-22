#!/usr/bin/env bash
# Render every kustomization under deploy/kubernetes and validate each object (C9).
#
# `make manifests` locally and the `manifests` CI job run this same file, so a manifest
# that builds here builds there. It needs `kubectl` (for its built-in kustomize) and
# `kubeconform` on PATH; CI installs both at pinned versions.
#
# SealedSecret, ExternalSecret and Argo's Application are skipped by name rather than by
# `-ignore-missing-schemas`: those three are the only custom resources this repository
# emits, and naming them means a fourth one added by accident fails instead of passing
# quietly.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out=$(mktemp -d -t crucible-manifests.XXXXXX)
trap 'rm -rf "$out"' EXIT HUP INT TERM

kubernetes_version="${CRUCIBLE_KUBECONFORM_K8S_VERSION:-1.31.0}"
targets=(base overlays/lab overlays/kind argocd)

for tool in kubectl kubeconform; do
  command -v "$tool" >/dev/null \
    || { echo "manifests: $tool is not on PATH; see docs/deployment.md" >&2; exit 2; }
done

for target in "${targets[@]}"; do
  name=${target//\//-}
  echo "manifests: building deploy/kubernetes/$target"
  kubectl kustomize "$root/deploy/kubernetes/$target" > "$out/$name.yaml"
  kubeconform -strict -summary \
    -kubernetes-version "$kubernetes_version" \
    -skip SealedSecret,ExternalSecret,Application \
    "$out/$name.yaml"
done

echo "manifests: asserting the security-relevant fields on the rendered objects"
cd "$root"
CRUCIBLE_RENDERED_MANIFESTS="$out" CRUCIBLE_MANIFESTS_REQUIRED=1 \
  "${UV:-uv}" run pytest tests/unit/test_deploy_manifests.py -q
