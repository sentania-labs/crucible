#!/usr/bin/env bash
# Build a disposable kind cluster and run the Kubernetes provider tier (18, 26).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
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

cleanup() {
  status=$?
  cleanup_failed=0
  trap - EXIT HUP INT TERM
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
  docker run --rm -v "$scratch:/cleanup" \
    busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0 \
    chmod -R a+rwX /cleanup >/dev/null 2>&1 || cleanup_failed=1
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

# Put a shim ahead of PATH so both this script and kind use the daemon selected
# by the same possibly wrapped Docker command as the other end-to-end tiers.
read -r -a docker_command <<< "${CRUCIBLE_E2E_DOCKER:-docker}"
if [ "${#docker_command[@]}" -eq 0 ]; then
  echo "e2e-kind: CRUCIBLE_E2E_DOCKER must name a Docker command" >&2
  exit 2
fi
host_docker=$(command -v docker)
for index in "${!docker_command[@]}"; do
  if [ "${docker_command[$index]}" = docker ]; then
    docker_command[index]=$host_docker
  fi
done
mkdir -p "$scratch/bin"
{
  printf '#!/usr/bin/env bash\nset -euo pipefail\nexec'
  printf ' %q' "${docker_command[@]}"
  printf ' "$@"\n'
} > "$scratch/bin/docker"
chmod 0755 "$scratch/bin/docker"
export PATH="$scratch/bin:$PATH"

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

registry_image='registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373'
docker network inspect kind >/dev/null 2>&1 || docker network create kind >/dev/null
registry_started=1
docker run -d --restart=no --network kind --name "$registry" \
  -p 127.0.0.1::5000 "$registry_image" >/dev/null
registry_port=$(docker port "$registry" 5000/tcp | awk -F: 'NR == 1 {print $NF}')
registry_ref="localhost:${registry_port}/crucible-worker:${run_id}"
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${registry_port}/v2/" >/dev/null; then
    break
  fi
  sleep 0.25
done
curl -fsS "http://127.0.0.1:${registry_port}/v2/" >/dev/null
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

calico="$scratch/calico.yaml"
curl -fsSL --retry 4 \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/calico.yaml \
  -o "$calico"
echo 'a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02  '"$calico" | sha256sum -c -
sed -i 's#192\.168\.0\.0/16#10.244.0.0/16#g' "$calico"
KUBECONFIG="$kubeconfig" kubectl apply -f "$calico" >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status daemonset/calico-node --timeout=180s
KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout=180s

KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kind/workers.yaml" >/dev/null
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
export CRUCIBLE_E2E_KIND_KUBECONFIG="$kubeconfig"
export CRUCIBLE_E2E_KIND_CANARY_LOG="$scratch/canary.log"
export CRUCIBLE_E2E_DOCKER_SOCKET="${CRUCIBLE_E2E_DOCKER_SOCKET:-/var/run/docker.sock}"
export KUBECONFIG="$kubeconfig"

cd "$root"
uv sync --frozen --quiet
uv run pytest tests/e2e/test_kind.py -q -m e2e
