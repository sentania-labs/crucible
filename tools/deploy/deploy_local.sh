#!/usr/bin/env bash
# Run a published Crucible release in normal mode on the dedicated rootless daemon.
#
# Why this exists (13, ADR 0004, C3 notes): the rootless daemon belongs to the
# `crucible` service user, and on the reference workstation the operator's home is
# mode 750, so that user cannot read the development tree. `make up` therefore cannot
# bring the normal-mode stack up from a working copy under /home. This target builds a
# deployment directory the service user owns, containing only files it can read, and
# runs a published, exactly pinned image from there. Nothing in that directory
# references the operator's home.
#
# It is not a development target. `make up` still runs the working tree, on a daemon
# that can see it.
#
# Host assumptions, stated rather than discovered at the wrong moment: GNU coreutils
# (`install -o -g`), glibc (`getent`, `id -gn`), curl 7.76 or newer
# (`--fail-with-body`), and a Compose plugin of at least 2.24 for the service user
# (`!reset`). Ubuntu 24.04 has all four; the script checks the last two.
set -euo pipefail

action="${1:-up}"
case "$action" in
  up|down) ;;
  *) echo "deploy-local: unknown action '$action'; expected 'up' or 'down'" >&2; exit 2 ;;
esac

SERVICE_USER="${CRUCIBLE_SERVICE_USER:-crucible}"
DEPLOY_DIR="${CRUCIBLE_DEPLOY_DIR:-/var/lib/crucible/deploy}"
CREDENTIAL_ROOT="${CRUCIBLE_CREDENTIAL_ROOT:-/var/lib/crucible/credentials}"
DEPLOY_IMAGE="${CRUCIBLE_DEPLOY_IMAGE:-}"
DEPLOY_PORT="${CRUCIBLE_DEPLOY_PORT:-8080}"
SPARK_ENDPOINT_URL="${CRUCIBLE_SPARK_ENDPOINT_URL:-}"
# The harness directories spec 12 names, plus the GitHub App's own directory (12, 25).
CREDENTIAL_DIRS="${CRUCIBLE_CREDENTIAL_DIRS:-claude_code codex agy github}"
# What the egress proxy is configured to permit and which subnet its source ACL names.
# The deployment records both so squid's rules and the application's declared allowlist
# cannot drift apart in the deployment directory the way they would if only squid.conf
# were copied. The Makefile is still the single definition of the values.
EGRESS_ALLOWLIST="${CRUCIBLE_EGRESS_ALLOWLIST_HOSTS:-}"
WORKERS_SUBNET="${CRUCIBLE_WORKERS_SUBNET_CIDR:-}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() { echo "deploy-local: $*" >&2; exit 2; }

service_uid="$(id -u "$SERVICE_USER" 2>/dev/null || true)"
[ -n "$service_uid" ] || die "no '$SERVICE_USER' service user; this target is only for the dedicated rootless daemon (13)"
service_group="$(id -gn "$SERVICE_USER" 2>/dev/null || true)"
[ -n "$service_group" ] || die "no primary group for '$SERVICE_USER'"
service_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[ -n "$service_home" ] || die "no home directory for '$SERVICE_USER'"

# Derived from the service user and from nothing else. CRUCIBLE_DOCKER_SOCKET is
# deliberately not honoured here: the Makefile exports it from a hardcoded `id -u
# crucible`, and its fallback is the host's rootful socket, so letting it through would
# let a stray environment variable put this deployment on the daemon whose compromise is
# a compromise of the host (13). The daemon this target uses is the one belonging to the
# user it runs as, by construction.
runtime_dir="/run/user/${service_uid}"
socket="${runtime_dir}/docker.sock"

sudo -n true 2>/dev/null || die "passwordless sudo is needed to own $DEPLOY_DIR as '$SERVICE_USER' and to run docker as it"

# Every docker invocation in this script goes through the service user, because the
# daemon is that user's and the deployment directory is that user's. The operator's own
# docker never touches this stack, and sudo's env_reset means nothing from the operator's
# environment reaches compose: the deployment's .env is the only source of its variables.
as_service_user() {
  sudo -n -u "$SERVICE_USER" env \
    HOME="$service_home" \
    XDG_RUNTIME_DIR="$runtime_dir" \
    DOCKER_HOST="unix://${socket}" \
    "$@"
}

# The override is applied when it is there. A run that failed between copying
# compose.yaml and writing the override still has to be brought down.
compose() {
  local -a files=(-f "$DEPLOY_DIR/compose.yaml")
  if sudo -n test -f "$DEPLOY_DIR/compose.deploy.yaml"; then
    files+=(-f "$DEPLOY_DIR/compose.deploy.yaml")
  fi
  as_service_user docker compose --project-directory "$DEPLOY_DIR" "${files[@]}" "$@"
}

if [ "$action" = "down" ]; then
  # Through sudo, like every other look at the deployment: the directory is mode 750 and
  # the operator is not the service user.
  sudo -n test -f "$DEPLOY_DIR/compose.yaml" || die "$DEPLOY_DIR has no deployment; run 'make deploy-local' first"
  # No --volumes: the database and the artifact root outlive a restart by design (13).
  # Workers are not compose services, so one that is still running keeps the
  # `crucible-workers` network alive and compose exits non-zero on the network removal.
  # That is the container being left alone, as 13 intends; `crucible-admin drain` is what
  # removes a worker.
  compose --profile "*" down
  echo "deploy-local: containers down, volumes kept (crucible-pg, crucible-artifacts)"
  exit 0
fi

[ -n "$DEPLOY_IMAGE" ] || die "set CRUCIBLE_DEPLOY_IMAGE, or DEPLOY_TAG when calling make"
case "$DEPLOY_IMAGE" in
  *:latest|*:latest@*) die "a deployment pins an exact tag; 'latest' is not one (docs/implementation-notes/release.md)" ;;
  *:*) ;;
  *) die "CRUCIBLE_DEPLOY_IMAGE must carry an exact version tag" ;;
esac
deploy_version="${DEPLOY_IMAGE##*:}"

# Through sudo: /run/user/<uid> is mode 700, so the operator cannot stat the socket,
# which is the same wall this whole target exists to work around.
sudo -n test -S "$socket" || die "no docker socket at $socket; the rootless daemon of '$SERVICE_USER' is not running (S9)"
[ -f "$repo_root/compose.yaml" ] || die "no compose.yaml at $repo_root"
[ -f "$repo_root/var/egress/squid.conf" ] || die "no var/egress/squid.conf; run 'make proxy-config' first"

# The override uses `!reset`, which Compose learned in 2.24. Saying so here is the
# preflight target's rule applied to this path: say what is wrong rather than letting
# compose fail with a YAML tag error after the deployment directory has been written.
compose_version="$(as_service_user docker compose version --short 2>/dev/null || true)"
[ -n "$compose_version" ] || die "'$SERVICE_USER' has no 'docker compose' plugin; install it for that user"
compose_major="${compose_version%%.*}"
compose_rest="${compose_version#*.}"
compose_minor="${compose_rest%%.*}"
if [ "${compose_major:-0}" -lt 2 ] || { [ "$compose_major" -eq 2 ] && [ "${compose_minor:-0}" -lt 24 ]; }; then
  die "Compose $compose_version for '$SERVICE_USER' is older than 2.24, which is where '!reset' arrived"
fi

echo "deploy-local: deployment directory $DEPLOY_DIR, image $DEPLOY_IMAGE, daemon $socket"

# 1. The directories. `install -d` creates the leading components too, so the parent is
#    created when it is missing and left exactly as it is when it already exists: naming
#    it explicitly would re-own and re-mode whatever CRUCIBLE_DEPLOY_DIR's parent happens
#    to be, which for /srv/crucible is /srv.
sudo -n install -d -o "$SERVICE_USER" -g "$service_group" -m 750 "$DEPLOY_DIR"
sudo -n install -d -o "$SERVICE_USER" -g "$service_group" -m 750 "$DEPLOY_DIR/var" "$DEPLOY_DIR/var/egress"

# 2. The credential root spec 12 and 25 expect: one empty directory per harness, mode
#    700, owned by the service user. This target creates the layout and nothing else;
#    a credential enters only through `crucible-admin credentials login` (25), which
#    points the harness's own interactive login at its directory here. The operator's
#    own harness directories are never read, copied or referenced (12).
sudo -n install -d -o "$SERVICE_USER" -g "$service_group" -m 700 "$CREDENTIAL_ROOT"
# Unquoted on purpose: CRUCIBLE_CREDENTIAL_DIRS is a space-separated list of names.
for dir in $CREDENTIAL_DIRS; do
  sudo -n install -d -o "$SERVICE_USER" -g "$service_group" -m 700 "$CREDENTIAL_ROOT/$dir"
done

# 3. The compose file and the generated egress allowlist, copied rather than referenced,
#    so the running stack does not depend on a path the service user cannot read.
sudo -n install -o "$SERVICE_USER" -g "$service_group" -m 640 "$repo_root/compose.yaml" "$DEPLOY_DIR/compose.yaml"

#    squid.conf is a bind mount into a running container, and `install` replaces the
#    file rather than rewriting it, so a running proxy would keep the old inode and the
#    old rules while this script reported success. The change is detected and the proxy
#    is recreated below.
squid_before="$(sudo -n sha256sum "$DEPLOY_DIR/var/egress/squid.conf" 2>/dev/null | cut -d' ' -f1 || true)"
sudo -n install -o "$SERVICE_USER" -g "$service_group" -m 640 "$repo_root/var/egress/squid.conf" "$DEPLOY_DIR/var/egress/squid.conf"
squid_after="$(sudo -n sha256sum "$DEPLOY_DIR/var/egress/squid.conf" | cut -d' ' -f1)"
egress_changed=0
if [ -n "$squid_before" ] && [ "$squid_before" != "$squid_after" ]; then
  egress_changed=1
fi

# 4. The override that pins the image. compose.yaml tracks `latest` and carries a
#    `build:` section for development; a deployment has neither a build context nor a
#    moving tag, so both are replaced here.
override="$(mktemp)"
trap 'rm -f "$override"' EXIT INT TERM
cat > "$override" <<YAML
# Generated by 'make deploy-local' (tools/deploy/deploy_local.sh). Rewritten on every
# run: edit the target, not this file.
#
# A deployment pins one exact version. The tag below is never 'latest': the pin is what
# makes a redeploy reproducible and a rollback one word, and it is why the repository
# itself carries no version-bump commit (docs/implementation-notes/release.md).
#
# 'build: !reset null' drops compose.yaml's development build section. There is no build
# context here, and a deployment runs the published artifact rather than a local rebuild.
services:
  migrate:
    image: ${DEPLOY_IMAGE}
    build: !reset null
  crucible:
    image: ${DEPLOY_IMAGE}
    build: !reset null
YAML
sudo -n install -o "$SERVICE_USER" -g "$service_group" -m 640 "$override" "$DEPLOY_DIR/compose.deploy.yaml"

# 5. The environment file, from .env.example, with a generated database password the
#    first time. The generation and the writing both happen inside one root shell that
#    uses only builtins for the value: the password is never a command-line argument
#    (/proc/<pid>/cmdline is world-readable), never in this script's environment, never
#    in make's output, and never printed. A later run keeps the password already there.
egress_json="["
sep=""
for host in $EGRESS_ALLOWLIST; do
  egress_json="${egress_json}${sep}\"${host}\""
  sep=","
done
egress_json="${egress_json}]"

sudo -n env \
  DEPLOY_DIR="$DEPLOY_DIR" \
  EXAMPLE="$repo_root/.env.example" \
  SERVICE_USER="$SERVICE_USER" \
  SERVICE_GROUP="$service_group" \
  DEPLOY_IMAGE="$DEPLOY_IMAGE" \
  DEPLOY_PORT="$DEPLOY_PORT" \
  SPARK_ENDPOINT_URL="$SPARK_ENDPOINT_URL" \
  SOCKET="$socket" \
  EGRESS_JSON="$egress_json" \
  WORKERS_SUBNET="$WORKERS_SUBNET" \
  bash -s <<'ENVGEN'
set -euo pipefail
umask 077
env_file="$DEPLOY_DIR/.env"
password=""
if [ -f "$env_file" ]; then
  # The pattern is on the command line; the value only ever crosses a pipe.
  password="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$env_file" | head -n 1)"
fi
if [ -z "$password" ] || [ "$password" = "CHANGE_ME" ]; then
  # Alphanumeric only: compose interpolates '$' in a .env value, and a password that
  # changes meaning between the file and the container is a debugging afternoon.
  #
  # Neither end of this pipeline can be killed by SIGPIPE: `head -c 512` exits on its
  # own after 512 bytes and `tr` reads that to EOF. The obvious shape,
  # `tr -dc ... < /dev/urandom | head -c 40`, is a trap: `head` closes the pipe as soon
  # as it has 40 characters, `tr` dies with 141, and `set -o pipefail` turns that into a
  # failed deployment before .env is ever written. It only bites on a fresh deployment,
  # because an existing .env skips this branch entirely. 512 random bytes yield about
  # 320 alphanumerics, so one pass is effectively always enough; the loop is there so
  # that an improbably poor draw extends rather than fails.
  password=""
  attempt=0
  while [ "${#password}" -lt 40 ] && [ "$attempt" -lt 8 ]; do
    attempt=$((attempt + 1))
    chunk="$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"
    password="${password}${chunk}"
  done
  password="${password:0:40}"
  [ "${#password}" -eq 40 ] || { echo "deploy-local: could not generate a password" >&2; exit 2; }
  echo "deploy-local: generated a new POSTGRES_PASSWORD in $env_file (not printed)"
else
  echo "deploy-local: kept the existing POSTGRES_PASSWORD in $env_file"
fi
tmp="$(mktemp "$DEPLOY_DIR/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT INT TERM
{
  echo "# Generated by 'make deploy-local' from .env.example. Owned by the service user,"
  echo "# mode 600. POSTGRES_PASSWORD is generated once and kept across runs: postgres"
  echo "# only reads it at initdb, so changing it here after the volume exists locks the"
  echo "# stack out of its own database."
  # Builtins only, so no process carries the value in its arguments.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      \#*|"") continue ;;
      POSTGRES_PASSWORD=*) printf '%s\n' "POSTGRES_PASSWORD=${password}" ;;
      CRUCIBLE_PORT=*) printf '%s\n' "CRUCIBLE_PORT=${DEPLOY_PORT}" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$EXAMPLE"
  echo "# The rootless daemon's socket, mounted into the socket proxy and nothing else."
  printf '%s\n' "CRUCIBLE_DOCKER_SOCKET=${SOCKET}"
  echo "# The pinned release, agreeing with compose.deploy.yaml."
  printf '%s\n' "CRUCIBLE_IMAGE=${DEPLOY_IMAGE}"
  echo "# The configured local model endpoint. Its presence enables the Spark route."
  [ -z "$SPARK_ENDPOINT_URL" ] || printf '%s\n' "CRUCIBLE_SPARK_ENDPOINT_URL=${SPARK_ENDPOINT_URL}"
  echo "# What the deployed squid.conf permits, so the application's declared allowlist"
  echo "# and the proxy's rules cannot drift apart in this directory (13, S6)."
  [ "$EGRESS_JSON" = "[]" ] || printf '%s\n' "CRUCIBLE_EGRESS_ALLOWLIST=${EGRESS_JSON}"
  [ -z "$WORKERS_SUBNET" ] || printf '%s\n' "CRUCIBLE_WORKERS_SUBNET=${WORKERS_SUBNET}"
} > "$tmp"
chown "$SERVICE_USER:$SERVICE_GROUP" "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$env_file"
trap - EXIT INT TERM
ENVGEN

# 6. Nothing in the deployment directory may name a path the service user cannot read:
#    that is the whole point of the directory, so it is asserted rather than assumed,
#    and "could not look" is distinguished from "found nothing".
scan_patterns=(-e '/home/')
[ -z "${HOME:-}" ] || scan_patterns+=(-e "$HOME")
scan_status=0
sudo -n grep -rIl "${scan_patterns[@]}" "$DEPLOY_DIR" >/dev/null || scan_status=$?
case "$scan_status" in
  0) die "$DEPLOY_DIR references a path under /home; the service user cannot read those" ;;
  1) ;;
  *) die "could not scan $DEPLOY_DIR for /home references (grep exit $scan_status)" ;;
esac

# 7. Bring it up as the service user, on its own daemon.
compose pull --quiet postgres docker-socket-proxy egress-proxy
compose pull --quiet crucible migrate
compose up -d --wait
if [ "$egress_changed" = 1 ]; then
  echo "deploy-local: the egress allowlist changed, recreating the proxy so it reads the new rules"
  compose up -d --force-recreate --wait egress-proxy
fi

# 8. Prove it is this deployment answering on that port, not something else that owns it:
#    /v1/health reports the version the image was built from, which must be the tag.
echo
echo "deploy-local: GET /v1/health"
health="$(curl -sS --fail-with-body "http://127.0.0.1:${DEPLOY_PORT}/v1/health")"
echo "$health"
case "$health" in
  *"\"version\":\"${deploy_version}\""*) ;;
  *) die "127.0.0.1:${DEPLOY_PORT} did not answer as ${DEPLOY_IMAGE}; something else owns that port" ;;
esac

echo
echo "deploy-local: GET /v1/ready"
curl -sS --fail-with-body "http://127.0.0.1:${DEPLOY_PORT}/v1/ready"
echo
echo
echo "deploy-local: running from $DEPLOY_DIR on the rootless daemon of '$SERVICE_USER'."
echo "  first use:   sudo -u $SERVICE_USER env HOME=$service_home XDG_RUNTIME_DIR=$runtime_dir \\"
echo "                 DOCKER_HOST=unix://$socket docker compose --project-directory $DEPLOY_DIR \\"
echo "                 -f $DEPLOY_DIR/compose.yaml -f $DEPLOY_DIR/compose.deploy.yaml \\"
echo "                 exec crucible crucible-admin token create --principal foundry --role orchestrator"
echo "  stop:        make deploy-local-down"
