# ADR 0004: Restricted Docker socket proxy for the local provider

Status: proposed, 2026-09-16.

## Context

Crucible must create sibling containers locally. Mounting the host Docker
socket into Crucible makes it root-equivalent on the host; mounting it
into workers is unacceptable.

## Decision

Only a socket-proxy container mounts the socket. Crucible talks HTTP to the
proxy with an endpoint allowlist limited to what the provider uses.
Crucible also refuses to emit dangerous create requests. Workers receive
neither the socket nor the proxy address and are on a network that cannot
reach them. The pattern is Docker-only; Kubernetes uses the API server with
a namespaced ServiceAccount.

## Consequences

A Crucible compromise is bounded to allowlisted images, two mount roots,
uid 1000, and dropped capabilities. The proxy is one more container and one
more pinned image to maintain. `exec` into workers is not available to
Crucible, which is intentional: observation is by logs and collected files.
