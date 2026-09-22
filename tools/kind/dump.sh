#!/usr/bin/env bash
set -u
kubeconfig=$1
canary_log=${2:-}
[ -f "$kubeconfig" ] || exit 0
export KUBECONFIG="$kubeconfig"
echo 'kind failure: pods'
kubectl get pods -A -o wide
echo 'kind failure: events'
kubectl get events -A --sort-by=.metadata.creationTimestamp
echo 'kind failure: network policies'
kubectl get networkpolicies -A -o yaml
echo 'kind failure: canary log'
if [ -n "$canary_log" ] && [ -f "$canary_log" ]; then
  cat "$canary_log"
else
  echo 'no readiness canary log was captured before the failure'
fi
