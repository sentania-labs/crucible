# Shared kind mechanics, sourced by tools/kind/e2e-kind.sh (C8b) and
# tools/kind/deploy-kind.sh (C9). One definition, two callers (sdlc skill).
#
# Nothing here creates a cluster or deletes one: the caller owns its own cleanup trap,
# because what has to be torn down differs between the two tiers.

# Calico, because kind's default CNI does not enforce egress NetworkPolicy and 26's
# readiness probe refuses to launch anything on a cluster that does not (C8b). The
# manifest is pinned by version and by the SHA-256 of the file itself, but the images
# it names were tag-only (67): a retag at quay.io would still pass that check and pull
# something the manifest was never proved against. Pin each by the digest quay.io
# reported for v3.32.2 on 2026-09-25 (docker-content-digest of the manifest list).
CRUCIBLE_CALICO_VERSION=v3.32.2
CRUCIBLE_CALICO_SHA256=a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02
CRUCIBLE_CALICO_CNI_DIGEST=sha256:0ef740bc587f25565905adf1d1f61a7faff0d571c449c6bdd789feed743d3ef7
CRUCIBLE_CALICO_NODE_DIGEST=sha256:99b03fe91e8bfbcb153ae65ef4b701b24ce541ffdd74ff314eb041096008f7fd
CRUCIBLE_CALICO_KUBE_CONTROLLERS_DIGEST=sha256:7870b67ebb13fabc3005252b44fe6e78b21635649bd3072b80afa1684b6565d0
CRUCIBLE_REGISTRY_IMAGE='registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373'

# Docker Hub serves the registry and busybox images this tier pulls, anonymously and
# rate limited on shared CI runners (68). mirror.gcr.io is Google's public pull-through
# cache of Docker Hub and needs no credential. Every Docker Hub image here is pinned by
# digest, so whichever of the two serves it, the bytes are the same.
CRUCIBLE_KIND_DOCKER_HUB_MIRROR=mirror.gcr.io
# shellcheck disable=SC2034 # used by the scripts that source this file
CRUCIBLE_BUSYBOX_IMAGE='busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0'

# The containerd patch that points the kind node's own Docker Hub pulls (busybox in
# deploy/kind/workers.yaml and the CoreDNS side container) at the mirror first, with
# Docker Hub itself as the fallback. Only for a cluster whose containerd does not set
# config_path, which cannot be combined with registry.mirrors.
# shellcheck disable=SC2034 # used by the scripts that source this file
CRUCIBLE_KIND_DOCKER_HUB_MIRROR_PATCH="[plugins.\"io.containerd.grpc.v1.cri\".registry.mirrors.\"docker.io\"]
      endpoint = [\"https://${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}\", \"https://registry-1.docker.io\"]"

# Pull a digest-pinned image onto the host daemon and print the reference that now
# resolves locally (68). A Docker Hub image is tried from the mirror first and from
# Docker Hub second on each attempt; four attempts, doubling from two seconds, ride out
# a short rate limit or a blip without masking a real outage.
crucible_kind_pull() {
  local image=$1 attempt delay=2 first candidate
  local -a candidates=("$image")
  first=${image%%/*}
  if [ "$first" = "$image" ]; then
    candidates=("${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}/library/${image}" "$image")
  elif [[ "$first" != *.* && "$first" != *:* && "$first" != localhost ]]; then
    candidates=("${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}/${image}" "$image")
  fi
  for attempt in 1 2 3 4; do
    for candidate in "${candidates[@]}"; do
      if docker pull -q "$candidate" >/dev/null 2>&1; then
        printf '%s\n' "$candidate"
        return 0
      fi
      echo "kind: pull of $candidate failed (attempt $attempt/4)" >&2
    done
    [ "$attempt" -eq 4 ] && break
    sleep "$delay"
    delay=$((delay * 2))
  done
  echo "kind: could not pull $image from any source" >&2
  return 1
}

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
# reaches it). Extra arguments are passed to `docker run`.
crucible_kind_start_registry() {
  local name=$1
  shift
  docker network inspect kind >/dev/null 2>&1 || docker network create kind >/dev/null
  local image
  image=$(crucible_kind_pull "$CRUCIBLE_REGISTRY_IMAGE")
  docker run -d --restart=no --network kind --name "$name" \
    -p 127.0.0.1::5000 "$@" "$image" >/dev/null
  CRUCIBLE_KIND_REGISTRY_PORT=$(docker port "$name" 5000/tcp | awk -F: 'NR == 1 {print $NF}')
  CRUCIBLE_KIND_REGISTRY_IP=$(docker inspect -f \
    '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$name")
  export CRUCIBLE_KIND_REGISTRY_PORT CRUCIBLE_KIND_REGISTRY_IP
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
  sed -i \
    -e 's#192\.168\.0\.0/16#10.244.0.0/16#g' \
    -e "s#quay.io/calico/cni:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/cni:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_CNI_DIGEST}#g" \
    -e "s#quay.io/calico/node:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/node:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_NODE_DIGEST}#g" \
    -e "s#quay.io/calico/kube-controllers:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/kube-controllers:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_KUBE_CONTROLLERS_DIGEST}#g" \
    "$calico"
  # A new manifest that spells an image differently would slip past the rewrite above.
  if grep -E '^[[:space:]]*image:' "$calico" | grep -v '@sha256:' >&2; then
    echo "kind: the Calico manifest names an image not pinned by digest (67)" >&2
    return 1
  fi
  KUBECONFIG="$kubeconfig" kubectl apply -f "$calico" >/dev/null
  KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status daemonset/calico-node --timeout=180s
  KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout=180s
}
