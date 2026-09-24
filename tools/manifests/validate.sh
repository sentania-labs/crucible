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

# The cluster budget the render-time resource check refuses to exceed (issue 93): CPU
# defaults to the lab's own stated shape (three 4-CPU nodes, issue 93), left as one
# variable because a deployment's real node count and size is not this repository's
# fact to assume. Memory has no such stated fact anywhere in this repository, so it is
# printed but not enforced unless a deployment passes its own budget.
cpu_budget="${CRUCIBLE_CLUSTER_CPU_BUDGET:-12}"
memory_budget_gi="${CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI:-}"

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
  if [[ "$target" == argocd ]]; then
    continue
  fi
  budget_args=(--label "$target" --cpu-budget "$cpu_budget")
  if [[ -n "$memory_budget_gi" ]]; then
    budget_args+=(--memory-budget-gi "$memory_budget_gi")
  fi
  "${UV:-uv}" run python "$root/tools/manifests/resource_budget.py" "$out/$name.yaml" "${budget_args[@]}"
done

echo "manifests: asserting the security-relevant fields on the rendered objects"
cd "$root"
CRUCIBLE_RENDERED_MANIFESTS="$out" CRUCIBLE_MANIFESTS_REQUIRED=1 \
  "${UV:-uv}" run pytest tests/unit/test_deploy_manifests.py -q
