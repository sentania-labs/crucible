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

The proxy reduces the API surface but does not validate request bodies, so
a compromised Crucible remains root-equivalent on the host through it. The
proxy plus Crucible's own create-request policy protect against Crucible
bugs and accidents, not against Crucible being hostile. Bounding the blast
radius further needs a rootless Docker daemon dedicated to Crucible or a
body-validating authorization layer; that choice is the operator's (spec 22,
Q11). The proxy is one more container to maintain. `exec` into workers is
not available to Crucible, which is intentional: observation is by logs and
collected files.
