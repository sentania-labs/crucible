# Shared kind mechanics, sourced by tools/kind/e2e-kind.sh (C8b) and
# tools/kind/deploy-kind.sh (C9). One definition, two callers (sdlc skill).
#
# Nothing here creates a cluster or deletes one: the caller owns its own cleanup trap,
# because what has to be torn down differs between the two tiers.

# Calico, because kind's default CNI does not enforce egress NetworkPolicy and 26's
# readiness probe refuses to launch anything on a cluster that does not (C8b).
CRUCIBLE_CALICO_VERSION=v3.32.2
CRUCIBLE_CALICO_SHA256=a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02
CRUCIBLE_REGISTRY_IMAGE='registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373'

# Put a shim ahead of PATH so both the caller and kind use the daemon selected by the
# same possibly wrapped Docker command as the other end-to-end tiers.
crucible_kind_docker_shim() {
  local scratch=$1
  local -a docker_command
  read -r -a docker_command <<< "${CRUCIBLE_E2E_DOCKER:-docker}"
  if [ "${#docker_command[@]}" -eq 0 ]; then
    echo "kind: CRUCIBLE_E2E_DOCKER must name a Docker command" >&2
    return 2
  fi
  local host_docker index
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
}

# Start the disposable OCI registry on the `kind` Docker network and wait for it to
# answer. Sets CRUCIBLE_KIND_REGISTRY_PORT (the host-loopback port) and
# CRUCIBLE_KIND_REGISTRY_IP (its address on the `kind` network, which is how a Pod
# reaches it). Extra arguments are passed to `docker run`. Also sets
# CRUCIBLE_KIND_NETWORK_CREATED=1 when the `kind` network did not already exist, so a
# caller that made it is the one that can try to clean it up (77): the name is shared
# by every kind cluster on the host, so a caller that finds it already there never owns
# its removal.
crucible_kind_start_registry() {
  local name=$1
  shift
  CRUCIBLE_KIND_NETWORK_CREATED=0
  if ! docker network inspect kind >/dev/null 2>&1; then
    docker network create kind >/dev/null
    CRUCIBLE_KIND_NETWORK_CREATED=1
  fi
  docker run -d --restart=no --network kind --name "$name" \
    -p 127.0.0.1::5000 "$@" "$CRUCIBLE_REGISTRY_IMAGE" >/dev/null
  CRUCIBLE_KIND_REGISTRY_PORT=$(docker port "$name" 5000/tcp | awk -F: 'NR == 1 {print $NF}')
  CRUCIBLE_KIND_REGISTRY_IP=$(docker inspect -f \
    '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$name")
  export CRUCIBLE_KIND_REGISTRY_PORT CRUCIBLE_KIND_REGISTRY_IP CRUCIBLE_KIND_NETWORK_CREATED
}

# Wait for a registry to answer /v2/ on the given base URL.
crucible_kind_await_registry() {
  local base=$1 curl_ca=${2:-}
  local -a curl_args=(-fsS)
  [ -n "$curl_ca" ] && curl_args+=(--cacert "$curl_ca")
  local _
  for _ in $(seq 1 40); do
    if curl "${curl_args[@]}" "${base}/v2/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  curl "${curl_args[@]}" "${base}/v2/" >/dev/null
}

# Install the pinned Calico and wait for the data plane and the node.
crucible_kind_install_calico() {
  local scratch=$1 kubeconfig=$2
  local calico="$scratch/calico.yaml"
  curl -fsSL --retry 4 \
    "https://raw.githubusercontent.com/projectcalico/calico/${CRUCIBLE_CALICO_VERSION}/manifests/calico.yaml" \
    -o "$calico"
  echo "${CRUCIBLE_CALICO_SHA256}  ${calico}" | sha256sum -c -
  sed -i 's#192\.168\.0\.0/16#10.244.0.0/16#g' "$calico"
  KUBECONFIG="$kubeconfig" kubectl apply -f "$calico" >/dev/null
  KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status daemonset/calico-node --timeout=180s
  KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout=180s
}
