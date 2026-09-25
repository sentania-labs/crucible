#!/usr/bin/env bash
# Exercises e2e-kind.sh's cleanup() against a stubbed docker, proving that a daemon
# which cannot confirm removal fails the run (correction to 77) while a busybox pull
# failure alone still does not (77's own intent, kept). Not wired into `make test`:
# there is no other shell-level test in this tree to share a runner with.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
failures=0

run_case() {
  local name=$1 stub_dir=$2 expect_status=$3 test_scratch status

  test_scratch=$(mktemp -d -t crucible-kind-cleanup-test.XXXXXX)
  set +e
  (
    PATH="$stub_dir:$PATH"
    export PATH
    export CRUCIBLE_KIND_TEST_HOOK=1
    # shellcheck source=tools/kind/e2e-kind.sh
    source "$root/tools/kind/e2e-kind.sh"
    trap - EXIT HUP INT TERM
    rm -rf "$scratch"
    cluster_created=1
    registry_started=1
    tag_created=1
    cluster="fake-cluster"
    registry="fake-registry"
    registry_ref="fake-ref"
    scratch="$test_scratch"
    kubeconfig="$scratch/kubeconfig"
    cleanup
  )
  status=$?
  set -e
  rm -rf "$test_scratch"

  if [ "$status" -eq "$expect_status" ]; then
    echo "PASS: $name (exit $status)"
  else
    echo "FAIL: $name: expected exit $expect_status, got $status" >&2
    failures=$((failures + 1))
  fi
}

daemon_down=$(mktemp -d -t crucible-kind-cleanup-stub.XXXXXX)
cat > "$daemon_down/docker" <<'EOF'
#!/bin/sh
echo "docker: cannot connect to the Docker daemon" >&2
exit 1
EOF
cat > "$daemon_down/kind" <<'EOF'
#!/bin/sh
exit 1
EOF
chmod +x "$daemon_down/docker" "$daemon_down/kind"

daemon_up_busybox_pull_fails=$(mktemp -d -t crucible-kind-cleanup-stub.XXXXXX)
cat > "$daemon_up_busybox_pull_fails/docker" <<'EOF'
#!/bin/sh
case "$1" in
  info) exit 0 ;;
  inspect) exit 1 ;;
  rm) exit 0 ;;
  image)
    case "$2" in
      inspect) exit 1 ;;
      rm) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  network)
    case "$2" in
      inspect) exit 1 ;;
      rm) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  run) exit 1 ;;
  pull) exit 1 ;;
  *) exit 1 ;;
esac
EOF
cat > "$daemon_up_busybox_pull_fails/kind" <<'EOF'
#!/bin/sh
case "$1" in
  delete) exit 0 ;;
  get) exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$daemon_up_busybox_pull_fails/docker" "$daemon_up_busybox_pull_fails/kind"

run_case "daemon unreachable during cleanup fails the run" "$daemon_down" 1
run_case "daemon reachable, busybox pull alone fails, run stays green" "$daemon_up_busybox_pull_fails" 0

rm -rf "$daemon_down" "$daemon_up_busybox_pull_fails"

if [ "$failures" -ne 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "all cleanup cases passed"
