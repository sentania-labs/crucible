#!/usr/bin/env bash
# Build a disposable kind cluster and run the Kubernetes provider tier (18, 26).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools/kind/cluster.sh
. "$root/tools/kind/cluster.sh"
run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-e2e-${run_id}"
registry="${cluster}-registry"
scratch=$(mktemp -d -t crucible-kind.XXXXXX)
kubeconfig="$scratch/kubeconfig"
cache="$scratch/reference-cache"
registry_ref=""
cluster_created=0
registry_started=0
tag_created=0
network_created=0

cleanup() {
  status=$?
  cleanup_failed=0
  # A second interrupt during cleanup must not abort it partway: `trap -` would
  # restore the default action (immediate termination), which is exactly what a
  # second Ctrl-C during a slow `kind delete cluster` would do (77). Ignoring the
  # signals here, not resetting them, is what lets cleanup run to completion.
  trap '' EXIT HUP INT TERM
  if [ "$status" -ne 0 ]; then
    "$root/tools/kind/dump.sh" "$kubeconfig" "$scratch/canary.log" || cleanup_failed=1
  fi
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
    if kind get clusters 2>/dev/null | grep -Fxq "$cluster"; then
      echo "e2e-kind cleanup: cluster $cluster remains" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$registry_started" -eq 1 ]; then
    docker rm -f "$registry" >/dev/null 2>&1 || :
    if docker inspect "$registry" >/dev/null 2>&1; then
      echo "e2e-kind cleanup: registry $registry remains" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$tag_created" -eq 1 ]; then
    docker image rm "$registry_ref" >/dev/null 2>&1 || :
    if docker image inspect "$registry_ref" >/dev/null 2>&1; then
      echo "e2e-kind cleanup: image tag $registry_ref remains" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$network_created" -eq 1 ]; then
    # The name is shared by every kind cluster on the host; `network rm` on one a
    # concurrent run still holds fails harmlessly (Docker refuses while an endpoint is
    # attached), so this only ever removes what became ours to remove.
    docker network rm kind >/dev/null 2>&1 || :
    if docker network inspect kind >/dev/null 2>&1; then
      echo "e2e-kind cleanup: network kind remains (a concurrent kind cluster may hold it)" >&2
    fi
  fi
  # A pull failure here is not a leak: it only means the chmod below did not run, and
  # the removal it was for is checked on its own right after (77). Treating the pull
  # itself as fatal failed an otherwise clean run whenever it could not reach the
  # registry.
  docker run --rm -v "$scratch:/cleanup" \
    busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0 \
    chmod -R a+rwX /cleanup >/dev/null 2>&1 || :
  rm -rf "$scratch" || cleanup_failed=1
  if [ -e "$scratch" ]; then
    echo "e2e-kind cleanup: scratch path $scratch remains" >&2
    cleanup_failed=1
  fi
  if [ "$cleanup_failed" -ne 0 ] && [ "$status" -eq 0 ]; then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

crucible_kind_docker_shim "$scratch"

# The provider resolves the tier's registry tag with the same crane the service image
# ships (108), fetched and checked against the Dockerfile's pin.
crane_bin=$("$root/tools/crane/fetch.sh")
PATH="$(dirname "$crane_bin"):$PATH"
export PATH

mkdir -p "$cache"
chmod 0777 "$cache"

worker_image=${CRUCIBLE_E2E_IMAGE:-$(
  awk -F= '$1 == "SCRIPT_HARNESS" {print $2}' "$root/images/manifest.env"
)}
if ! docker image inspect "$worker_image" >/dev/null 2>&1; then
  echo "e2e-kind: images/manifest.env pins $worker_image but the host daemon lacks it" >&2
  echo "e2e-kind: run 'make e2e-image' first" >&2
  exit 2
fi

registry_started=1
crucible_kind_start_registry "$registry"
network_created=$CRUCIBLE_KIND_NETWORK_CREATED
registry_port=$CRUCIBLE_KIND_REGISTRY_PORT
registry_ref="localhost:${registry_port}/crucible-worker:${run_id}"
crucible_kind_await_registry "http://127.0.0.1:${registry_port}"
tag_created=1
docker tag "$worker_image" "$registry_ref"
docker push "$registry_ref" >/dev/null

cat > "$scratch/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: $cluster
networking:
  disableDefaultCNI: true
  podSubnet: 10.244.0.0/16
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: $cache
        containerPath: /crucible-kind-cache
kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    podPidsLimit: 512
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors."localhost:${registry_port}"]
      endpoint = ["http://${registry}:5000"]
EOF

cluster_created=1
kind create cluster --config "$scratch/kind.yaml" --kubeconfig "$kubeconfig" --wait 0s

node="${cluster}-control-plane"
for address in 10.0.0.1 172.16.0.1 192.168.0.1 100.64.0.1 169.254.169.254; do
  docker exec "$node" ip address add "$address/32" dev lo
done

crucible_kind_install_calico "$scratch" "$kubeconfig"

KUBECONFIG="$kubeconfig" kubectl create namespace crucible >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n crucible create serviceaccount crucible-supervisor >/dev/null
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kind/workers.yaml" >/dev/null
# The tier consumes the deployed RBAC files directly. A provider change needing another
# verb therefore exercises the same Role and RoleBinding that the deployment uses.
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/role.yaml" >/dev/null
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/rolebinding.yaml" >/dev/null
supervisor_kubeconfig="$scratch/supervisor-kubeconfig"
api_server=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
ca_data=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
supervisor_token=$(KUBECONFIG="$kubeconfig" kubectl -n crucible create token crucible-supervisor)
ca_file="$scratch/cluster-ca.crt"
printf '%s' "$ca_data" | base64 -d > "$ca_file"
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-cluster kind \
  --server="$api_server" --certificate-authority="$ca_file" --embed-certs=true >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-credentials crucible-supervisor \
  --token="$supervisor_token" >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-context crucible-supervisor \
  --cluster=kind --user=crucible-supervisor --namespace=crucible-workers >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config use-context crucible-supervisor >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system patch deployment coredns --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"wrong-port-www","emptyDir":{}}},
  {"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"wrong-port-http","image":"busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0","command":["sh","-c","mkdir -p /www; echo dns-wrong-port > /www/index.html; exec httpd -f -p 18080 -h /www"],"securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"runAsNonRoot":true,"runAsUser":1000,"capabilities":{"drop":["ALL"]}},"volumeMounts":[{"name":"wrong-port-www","mountPath":"/www"}]}}
]' >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system patch service kube-dns --type=json \
  -p='[{"op":"add","path":"/spec/ports/-","value":{"name":"wrong-port","port":443,"protocol":"TCP","targetPort":18080}}]' >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status deployment/coredns --timeout=180s
KUBECONFIG="$kubeconfig" kubectl -n kube-system wait --for=condition=Ready pod/range-http --timeout=90s
KUBECONFIG="$kubeconfig" kubectl -n crucible-kind-peer wait --for=condition=Ready pod/peer-http --timeout=90s
KUBECONFIG="$kubeconfig" kubectl -n crucible-workers wait \
  --for=jsonpath='{.status.phase}'=Bound pvc/crucible-reference-cache --timeout=90s

# Required even though the real registry path below is what proves resolution and pull.
kind load docker-image "$worker_image" --name "$cluster"

dns_ip=$(KUBECONFIG="$kubeconfig" kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}')
peer_ip=$(KUBECONFIG="$kubeconfig" kubectl -n crucible-kind-peer get service peer-http -o jsonpath='{.spec.clusterIP}')
api_ip=$(KUBECONFIG="$kubeconfig" kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')

export CRUCIBLE_E2E_KIND=1
export CRUCIBLE_E2E_KIND_CACHE="$cache"
export CRUCIBLE_E2E_KIND_CLUSTER="$cluster"
export CRUCIBLE_E2E_KIND_DNS_IP="$dns_ip"
export CRUCIBLE_E2E_KIND_PEER_IP="$peer_ip"
export CRUCIBLE_E2E_KIND_API_IP="$api_ip"
export CRUCIBLE_E2E_KIND_REGISTRY="$registry_ref"
export CRUCIBLE_E2E_KIND_KUBECONFIG="$supervisor_kubeconfig"
export CRUCIBLE_E2E_KIND_CANARY_LOG="$scratch/canary.log"
export CRUCIBLE_E2E_DOCKER_SOCKET="${CRUCIBLE_E2E_DOCKER_SOCKET:-/var/run/docker.sock}"
export KUBECONFIG="$kubeconfig"

cd "$root"
uv sync --frozen --quiet
# CRUCIBLE_E2E_KIND_PYTEST_ARGS narrows the run (for example `-k login -s`) when one
# case is being proven on its own cluster; CI leaves it empty and runs the whole tier.
read -r -a extra_args <<< "${CRUCIBLE_E2E_KIND_PYTEST_ARGS:-}"
uv run pytest tests/e2e/test_kind.py -q -m e2e "${extra_args[@]}"
