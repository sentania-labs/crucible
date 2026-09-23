#!/usr/bin/env bash
# The kind proof for issue 93 (contract FDY-0107, requirement 8): a ResourceQuota that
# mirrors the lab's tight CPU budget refuses the old pod shape (request equal to limit)
# and admits and schedules the new shape (request a fraction of the limit).
#
# A disposable kind cluster, unique name, deleted on exit even on failure (sdlc skill's
# kind pattern). No Calico, no registry, no Crucible process: this proof is about one
# container's `resources` block against one ResourceQuota, not the whole provider.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-req-${run_id}"
namespace="crucible-verify"
cluster_created=0

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

echo "verify: creating kind cluster $cluster"
kind create cluster --name "$cluster" --wait 0s
cluster_created=1
kubectl wait --for=condition=Ready nodes --all --timeout=180s

echo "verify: pulling busybox into the cluster so scheduling is the only thing tested"
kind load docker-image busybox:1.36 --name "$cluster"

kubectl create namespace "$namespace"
kubectl -n "$namespace" create serviceaccount crucible-worker

# Mirrors the lab: a tight requests.cpu budget with headroom on limits.cpu, the same
# shape as deploy/kubernetes/base/workers/resourcequota.yaml (requests well below
# limits so a Burstable pod can still schedule).
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: verify-budget
  namespace: $namespace
spec:
  hard:
    requests.cpu: "1"
    requests.memory: 1Gi
    limits.cpu: "4"
    limits.memory: 4Gi
EOF

old_pod=$("${UV:-uv}" run python "$root/tools/kind/render_request_pod.py" \
  --name old-shape --namespace "$namespace" --cpu-request-fraction 1.0)
new_pod=$("${UV:-uv}" run python "$root/tools/kind/render_request_pod.py" \
  --name new-shape --namespace "$namespace" --cpu-request-fraction 0.5)

echo "verify: applying the old shape (request equals the 2-CPU limit)"
if echo "$old_pod" | kubectl apply -f - 2>old_shape.err; then
  echo "verify: FAIL: the old shape was admitted; it should have exceeded the quota" >&2
  cat old_shape.err >&2
  exit 1
fi
echo "verify: old shape refused, as expected:"
cat old_shape.err
if ! grep -q "exceeded quota" old_shape.err; then
  echo "verify: FAIL: refusal was not the quota (see above)" >&2
  exit 1
fi
rm -f old_shape.err

echo "verify: applying the new shape (request is half the 2-CPU limit)"
echo "$new_pod" | kubectl apply -f -

echo "verify: waiting for the new shape to schedule and run"
kubectl -n "$namespace" wait --for=condition=Ready pod/new-shape --timeout=60s
kubectl -n "$namespace" get pod new-shape -o wide
kubectl -n "$namespace" get pod new-shape -o jsonpath='{.spec.containers[0].resources}{"\n"}'

echo "verify: PASS: the old (request == limit) shape does not schedule under the lab's"
echo "verify: budget, and the new (request < limit) shape does."
