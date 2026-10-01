# Codex credential renewer

FDY-0213 replaces Codex's per-attempt writable login copy with one-writer renewal.
The supervisor alone stores and changes `auth.json`. It refreshes at 75 percent of the
JWT access-token lifetime or on demand, coalescing callers under a process lock and a
one-minute freshness window.

Workers receive a per-attempt read-only JSON file with only `access_token`,
`account_id`, and `expires_at`. `crucible-codex-host` starts `codex app-server`, enables
the experimental API during initialization, supplies those values through
`chatgptAuthTokens`, and re-reads the file when app-server requests a refresh. Docker
uses atomic replacement. Kubernetes patches each attempt Secret; the 90-second host
wait covers kubelet's eventual projection. Neither path uses a shared writable volume.

`invalid_grant` and `refresh_token_reused` are terminal. The renewer marks the login
dead, records one failure and raises one wake. It does not retry the grant. Existing
workers retain their current access token, while launch admission refuses new Codex
workers until an operator logs in again.

An explicit `rw-narrow` credential mount selects the previous `codex exec` copy and
sync-back path for rollback. Local-endpoint Codex keeps its API-key launch path.

PR 327 correction: supervisor launch specs and credential probes carry the effective
mount mode, including a Kubernetes mode configured without a directory. Revision 0034
accepts `renewer` credential observations and restores the original constraint on
downgrade after clearing observations that the old schema cannot represent. Renewal
captures the service loop when wired, dispatching projection updates and refresh
events back to that loop from the retention thread. Explicit `rw-narrow` mode neither
constructs nor schedules a renewer, preserving the legacy path as the sole writer.
