# 25. Crucible administration: admin API, crucible-admin CLI, credential onboarding

Harness credentials, harness availability, worker-image versions, execution
providers, GitHub App configuration, and operational health are Crucible
administrative concerns. Foundry may detect and report that a capability is
unavailable; it never stores or manages a Crucible credential. Another
orchestrator or a person can administer Crucible through the same surface
without Foundry.

## Shape

- **Versioned admin API** under `/v1/admin`, admin role only, generated
  into the same OpenAPI document. Every mutation is an event with the
  principal, before-and-after summary (never a value), and reason.
- **`crucible-admin` CLI** calls the same application services as the API
  (`crucible/application/admin/*`), not a private path; the CLI is a
  client of the service layer running in-process (local mode) or against
  the API (remote mode). Anything the CLI can do, the API can do, and the
  audit event is identical.
- **Sanitized status** only. No response, log line, event payload, table
  row, or artifact ever carries a credential value, a token, a key, or a
  file's contents. Validation results are booleans, enumerations,
  timestamps, hashes of non-secret metadata, and version strings.
- **No web admin UI before the worker-supervision readiness milestone.** A
  later interface calls the admin API; it owns no state. Section
  "Designed for a later interface" lists what the API must expose so that
  portal needs nothing else.

## Status model

`GET /v1/admin/status` returns one document; each part is also its own
resource.

| Part | Fields |
|---|---|
| `harnesses[]` | name, enabled, adapter supported range, images known (reference, harness version, digest, promotion state), credential status (below), concurrency limit and current use, last launch outcome |
| `credentials[harness]` | `state`: `absent`, `configured` (files present, shape unchecked), `invalid` (shape or probe failed), `validated` (probe succeeded); `mount_mode` (`ro`, `rw-narrow`); `last_validated_at`; `last_auth_failure_at` and its exit class; `refresh_verified` (bool, from the compatibility test); `source_fingerprint` (sha256 of the file **names and sizes**, never contents); `session_compatibility`: `unverified`, `verified`, `failed` |
| `providers[]` | name, capabilities, `health`: `ok`, `degraded`, `unavailable` with detail (daemon reachable, proxy reachable, network present, disk headroom) |
| `github` | App id and slug (public), key present (bool), key fingerprint (sha256 of the public key), installations visible, per registered repository: installation covers it, last successful token mint, last API failure, webhook enabled |
| `supervisor` | as `GET /supervisor` (lease, last tick, last error) |
| `workers` | active attempts with task, harness, model, image digest, started_at, last heartbeat |
| `tasks` | counts by state; lists for `blocked`, `pre_pr_gates_failed`, `publish_failed`, `ci_certification_failed`, `head_diverged` |
| `wakes` | pending count per principal, oldest pending |
| `retention` | last run, actions taken, next due, bytes reclaimed |
| `audit` | cursor into admin events |

## Operations (each is an application service; API and CLI are thin)

| Operation | API | CLI | Notes |
|---|---|---|---|
| list harnesses | `GET /admin/harnesses` | `harnesses list` | |
| disable or enable a harness | `POST /admin/harnesses/{name}/disable` and `/enable` | `harnesses disable|enable` | configuration retained; running attempts finish; new launches refused with a wake |
| validate a credential | `POST /admin/credentials/{harness}/validate` | `credentials validate --harness` | shape check of the named auth files, then a bounded probe (below); returns state and timestamps only |
| bounded auth probe | `POST /admin/credentials/{harness}/probe` | `credentials probe --harness` | launches the hardened worker image with the credential mounted, runs a one-line prompt with a 120 s timeout, records exit class, harness version, image digest, whether auth files changed (by hash), removes everything; never shows output beyond the exit class |
| onboard a credential | `POST /admin/credentials/{harness}/login` (starts) | `credentials login --harness` | interactive flow below; the API form returns the device or browser URL and polls for completion |
| rotate or replace a credential source | `POST /admin/credentials/{harness}/rotate` | `credentials rotate --harness` | new directory prepared and validated first; swap is atomic (rename); previous directory retained for `credential_retention_hours` then shredded; every step an event |
| remove a credential | `POST /admin/credentials/{harness}/remove` | `credentials remove --harness` | harness becomes `absent`; files shredded |
| list images and promote | `GET /admin/images`, `POST /admin/images/{digest}/promote` | `images list|promote` | 13 |
| provider health | `GET /admin/providers` | `providers status` | |
| GitHub health | `GET /admin/github`, `POST /admin/github/check` | `github status|check` | check mints a token per registered repository and discards it |
| register a repository | `PUT /admin/repositories/{name}` | `repositories register` | 04 |
| audit | `GET /admin/audit?cursor=` | `audit tail` | admin events only |

Every mutation requires a `reason` string, records the principal, and is
refused when the supervisor lease is not held by a live instance (so a
stale instance cannot administer).

## Credential onboarding workflow

`crucible-admin credentials login --harness <claude_code|codex|agy>`:

1. Create or select the dedicated Crucible credential directory for the
   harness under the configured credential root (`credentials.<harness>.path`,
   mode 700, owned by the Crucible service user).
2. Run that harness's supported interactive login with its configuration
   and home variables pointed **directly** at that directory: Claude Code
   with `CLAUDE_CONFIG_DIR`, Codex with `CODEX_HOME`, AGY with its config
   directory variable (recorded by the adapter's `credential_spec`). The
   operator's daily-use directory is never read, copied, or referenced.
3. Complete browser or device authorization with the operator. Where the
   CLI offers a device-code flow the command prints the URL and code so the
   operator can finish from any machine; otherwise it prints the browser
   URL.
4. Validate the resulting credential structure: the named auth files
   exist, parse, and carry the expected fields; nothing is printed.
5. Run the bounded auth probe in the hardened worker image.
6. Record only: validation result, `mount_mode`, file names and sizes,
   harness version, image digest, timestamp, and whether the auth files
   changed during the probe (which sets `refresh_requires_rw`).
7. Report whether the credential requires writable refresh state and set
   `mount_mode` accordingly (`rw-narrow` when the probe changed files).
8. Remove all probe resources.
9. Mark `session_compatibility: unverified` until the daily-session
   compatibility test (21, S1b) passes; a harness is not enabled for
   normal workers before that.

"Dedicated Crucible credential" means dedicated authentication state for
Crucible, not necessarily a separate subscription or account. Whether a
provider allows several simultaneous authenticated sessions on one
subscription account is documented per harness in 07 once S1b establishes
it; until then the field reads "unverified".

## Designed for a later interface

The admin API must expose, without any other data source: harness status;
credential onboarding and validation status (including an in-progress
login's device URL); worker-image versions and promotion state; provider
health; GitHub App and repository connectivity; active workers; failed or
blocked tasks; pending Foundry wakes; retention and cleanup status; audit
events with cursors. A portal that combines Foundry chat, task and Kanban
views, Crucible execution views, and this administrative surface is a
client of the two services and owns none of their state; it never
collapses the authority boundary between them.

## What Foundry may do

Read `/v1/admin/status` parts that its role permits (orchestrator role
gets `harnesses`, `providers`, `github` health, `workers`, `tasks`, `wakes`
read-only through `GET /v1/capabilities`), and report to the operator that
a harness is disabled, a credential is invalid or unverified, or a provider
is unavailable. It never calls the mutation endpoints.
