# ADR 0004: Rootless Docker daemon preferred; restricted socket proxy in front of whichever daemon is used

Status: accepted with the operator's decision 13, 2026-09-16 (supersedes the 0.2 proposal).

## Context

Crucible must create sibling containers locally. Anything that can talk to
a Docker socket is root-equivalent on that daemon's host user. A proxy can
narrow the API surface but cannot validate request bodies, so a proxy alone
leaves a compromised Crucible root-equivalent when the daemon is the host's
rootful one.

## Decision

1. Run the rootless-Docker spike (S9) first in C0. If a dedicated rootless
   daemon for a Crucible service user works reliably on the development
   workstation, it is the default local arrangement from C3: a full escape
   yields an unprivileged user.
2. If S9 fails, the host socket is used temporarily through the proxy,
   with the root-equivalent trust risk recorded in the readiness report
   and revisited before any multi-user deployment.
3. In both cases only the socket-proxy container mounts the socket;
   Crucible talks HTTP to the proxy with an endpoint allowlist, refuses to
   emit dangerous create requests, and no worker, collector, verifier, or
   publisher container ever receives the socket or the proxy endpoint.
4. The pattern is Docker-only; Kubernetes uses the API server with a
   namespaced ServiceAccount and admission policy.

## Consequences

One extra daemon to install and keep patched in the preferred arrangement,
and some Docker features unavailable in rootless mode (privileged ports,
some storage drivers, cgroup limits contingent on host delegation). The
proxy remains a tripwire, not a boundary. `exec` into workers is not
available to Crucible, which is intentional.
