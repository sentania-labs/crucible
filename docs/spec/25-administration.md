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
  the API (remote mode). Every operation in the table below exists on
  both, with the same audit event, and parity tests drive both entry
  points. Four operations are CLI-only by design because they need the
  database or filesystem directly and run before or beside the service:
  `migrate`, `import` (bootstrap, which also has its verify and commit API
  in 15), `export`, and `token create`; they are audited the same way and
  the API exposes their status (migration head, import state, token list
  without secrets) but not their execution. Of those four, `migrate` and
  `token create` exist today; `import` and `export` are named here and in 14
  but arrive with the bootstrap ledger (15) in C6, and the CLI's own help
  states the gap rather than asserting commands that are not there.
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
| `harnesses[]` | name, both enablement gates with the reason each carries, adapter supported range, images known (reference, harness version, digest, promotion state), credential status (below), concurrency limit and current use, last launch outcome (a probe records itself there as `probe:<exit class>`, or `probe:inconclusive:<cause>` when it decided nothing) |
| `credentials[harness]` | `state`: `absent`, `configured` (files present, shape unchecked), `invalid` (the shape check failed, or a probe observed the provider refusing the credential; never a probe that merely did not finish), `validated` (a conclusive probe ran the credential); `mount_mode` (`ro`, `rw-narrow`); `last_validated_at`; `last_auth_failure_at` and its exit class (set only by those two conclusive outcomes); `refresh_verified` (bool, from the compatibility test); `source_fingerprint` (sha256 of the file **names and sizes**, never contents); `session_compatibility`: `unverified`, `verified`, `failed` |
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
| disable or enable a harness | `POST /admin/harnesses/{name}/disable` and `/enable` | `harnesses disable|enable` | flips the administrator's flag only; configuration retained; running attempts finish; new launches refused with a wake |
| validate a credential | `POST /admin/credentials/{harness}/validate` | `credentials validate --harness` | shape check of the named auth files, then the bounded probe (below); returns state, timestamps, and the probe's `conclusive` and `cause`, never a verdict the run did not support |
| bounded auth probe | `POST /admin/credentials/{harness}/probe` | `credentials probe --harness` | launches the promoted worker image with the credential mounted, runs a one-line prompt with a 120 s timeout, records exit class, conclusiveness and cause, harness version, image digest, whether auth files changed (by hash), mount mode and duration, removes everything; never shows output beyond the exit class |
| onboard a credential | `POST /admin/credentials/{harness}/login` (starts) | `credentials login --harness` | interactive flow below, and a local-mode operation on a host that carries the harness CLI; the API form returns the device or browser URL and polls for completion where it can run at all |
| rotate or replace a credential source | `POST /admin/credentials/{harness}/rotate` | `credentials rotate --harness` | the operator's prepared directory is shape-checked, copied in, and left exactly as it was found; the swap is two renames; the previous directory is retained for `credential_retention_hours` then shredded; a failed swap rolls back; every step an event |
| remove a credential | `POST /admin/credentials/{harness}/remove` | `credentials remove --harness` | harness becomes `absent`; the directory is shredded at once rather than retained, because the operator said remove, and the harness is disabled with that reason |
| list images and promote | `GET /admin/images`, `POST /admin/images/{digest}/promote` | `images list|promote` | 13; one default per harness, and promoting a digest marks the previous default `retained`. The probe and the live tiers run the promoted image |
| provider health | `GET /admin/providers` | `providers status` | |
| GitHub health | `GET /admin/github`, `POST /admin/github/check` | `github status|check` | check mints a token per registered repository and discards it |
| register a repository | `PUT /admin/repositories/{name}` | `repositories register` | 04; an administrative mutation like any other, guarded and audited here, with the previous registration as the before summary. 04's own `PUT /repositories/{name}` is a different, non-administrative surface |
| audit | `GET /admin/audit?cursor=` | `audit tail` | admin events only; the cursor is the scan position and always moves forward, so a long stretch of non-administrative events cannot strand the pages behind it |

Every mutation requires a non-empty `reason` string, records the principal,
and is refused when the supervisor lease is not held by a live instance (so
a stale instance cannot administer). One guard applies both rules, so no
operation can carry only one of them, and they bind every state-changing row
of the table above without exception: registering a repository and finishing
a login are mutations too, and a login's completion writes
`session_compatibility` and clears `last_validated_at`, which is exactly the
kind of change the rules exist for. A reason is a string an operator wrote:
an absent, blank, or non-string body value is not one.

A refusal is itself recorded, as an `admin_refused` event naming the
operation and why it was refused, written through a unit of work of its own
because the refusal ends the caller's transaction. Recording it is best
effort: a refusal is never made worse by a failure to record it.

The reason and the rest of a mutation's payload are served back by `GET
/admin/audit`, so the whole assembled payload is scanned before it is
written. A secret-shaped reason is refused with 422 naming the pattern and
never the value, rather than being stored or silently redacted, so an
operator who pasted a token into a reason field learns that it landed
nowhere (12).

## The bounded probe: conclusive or inconclusive

The probe is not a special path. It builds the same container an attempt
builds (the harness's promoted image, the per-attempt credential copy of 12,
the egress proxy with an empty task allowlist so only the adapter's own
endpoints are reachable, the same CPU and memory bounds, 120 s), runs the
adapter's own launch for a one-line prompt, and classifies the exit through
the adapter. Its model is the cheapest enabled model for that harness in the
routing policy in force (05b); a harness that takes a model flag and has no
enabled model is a refusal that says so, never a guess, because the CLIs
reject an unknown model id. When a daemon carries several labelled images for
a harness and none is promoted, the probe refuses and says to promote one.
Stdout and stderr tails are classified and dropped. Everything the probe
created is removed on every path, including a failure before the credential
is seeded.

The probe's record carries `conclusive` and, when it is not, a `cause`.
Only two outcomes say anything about the credential: a run that completed,
and a run whose tail the adapter read as an authentication failure. Only the
shape check failing or that observed authentication failure sets
`last_auth_failure_at` and makes the state `invalid`. Everything else (a
timeout, a crash, a blocked or lost run, a quota refusal, or a provider error
that means the run never started) is inconclusive: it records itself with its
cause, leaves the credential's state exactly as it was, and comes back from
`validate` as `validated: false, conclusive: false, cause: "<cause>"` rather
than as a verdict. A provider that refuses to answer is a record with a cause,
not a server error.

The reason for the split is operational. The probe is bounded, and a bounded
run that ends for any reason other than the provider's answer is evidence
about the run, not about the credential. Under a rule where any unsuccessful
probe means `invalid`, a loaded daemon or a slow model response would condemn
a working credential and take that harness out of service on latency, and the
operator reading `invalid` would go looking for a revoked token that does not
exist. The 120 s bound stands; conclusiveness is what makes it safe.

## Harness enablement: two gates

A harness is available for a launch only when **both** gates say so, and
each carries its own reason string:

1. **Configuration**, the operator's static gate: a `[harnesses.<name>]`
   section exists in Crucible's configuration. Changing it is an operator
   edit and a restart.
2. **The administrator's flag**, the runtime gate: the harness's row, which
   `harnesses enable` and `harnesses disable` flip through the admin
   surface without touching configuration.

Either gate saying no refuses the launch, and the refusal names which gate
and the reason it carries. `GET /admin/harnesses` reports both gates and
both reasons, so "disabled" is never ambiguous about who disabled it. A
refusal is terminal for that attempt: it ends as `environment` with a
`harness_refused` event and a `harness_unavailable` wake, and the retry
rule skips it, because the same refusal would come back (16). A harness
disabled at the time a contract is submitted is a contract problem then
(05b), not a refusal a task discovers later.

## Credential onboarding workflow

`crucible-admin credentials login --harness <claude_code|codex|agy>`:

0. **Where it runs.** The flow drives the harness's own CLI in a pty on the
   host the flow runs on. By 13 each harness CLI lives only in its worker
   image and the Crucible service image carries none of them, so on a normal
   deployment the API form cannot work: it resolves the executable first and
   refuses with that reason, leaving no session behind so a later attempt is
   accepted. Login is therefore a local-mode operation on a host that has the
   harness CLI, and the subcommand help, the route's description, and the
   module say so. Running the flow inside the promoted harness image, the way
   the probe runs, would need the pty and the operator's pasted code relayed
   through the daemon's attach stream; that is a phase of its own and is the
   named follow-up that would make the API form work anywhere.
1. Create or select the dedicated Crucible credential directory for the
   harness under the configured credential root (`credentials.<harness>.path`,
   mode 0700, owned by the Crucible service user; 13 for who creates it). A
   directory that still passes the shape check is **not** overwritten: the
   login refuses unless the caller explicitly asks to replace it, and a
   replacement retires the existing directory under rotation's retained name
   so the retention sweep shreds it on schedule. Silently truncating a
   working credential is the one thing rotation is careful about, and login
   is held to the same rule.
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
   `mount_mode` to the stricter of the adapter's declared minimum (07; all
   three harnesses declare `rw-narrow`) and what the probe observed
   (`rw-narrow` when the probe changed files). A probe that changes nothing never
   lowers the adapter's minimum.
8. Remove all probe resources.
9. Mark `session_compatibility: unverified` until the daily-session
   compatibility test (21, S1b) passes for that harness, Claude Code
   included; a harness is not enabled for normal workers before that.

"Dedicated Crucible credential" means dedicated authentication state for
Crucible, not necessarily a separate subscription or account. Whether a
provider allows several simultaneous authenticated sessions on one
subscription account is documented per harness in 07 once S1b establishes
it; until then the field reads "unverified".

The device or browser code the CLI prints has a window, and the flow states
it up front rather than leaving the operator to discover it: Codex's device
code lasts fifteen minutes, AGY's sixty seconds, and Claude Code's
`setup-token` takes the pasted code and prints the long-lived token, which
the driver captures into the credential file mode 0600 and never displays.
The CLI's own output is not echoed.

## Rotation and removal

Rotation takes a directory the operator prepared themselves. Crucible
shape-checks it, **copies** it into the configured credential root as a
staged directory, renames the live directory aside as the retained one, and
renames the staged copy into place. The operator's source is left exactly as
it was found, and the response and the event say so: a mistyped source path
can no longer cost the operator data, and nothing Crucible destroys is
outside the configured credential root. Even inside that root nothing is
destroyed at once: the retained directory goes on the retention schedule
(`[admin] credential_retention_hours`, 24 by default) and the supervisor's
retention tick shreds it when it is old enough.

The swap is two renames, so it can fail between them. When it does, the
retained directory is renamed back, the staged copy is shredded, the failure
is recorded through a unit of work of its own (the caller's is about to roll
back), and the refusal says that it was rolled back. A rotation that fails
leaves what it found, and never a configured path with nothing behind it.

Removal is the exception to retention: the directory is shredded at once,
because the operator asked for the credential to be gone, and the harness is
disabled with `credential removed: <reason>`. Running attempts finish, as for
any disable. Shredding is complete or it says so (12).

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
read-only through `GET /v1/capabilities`, reduced to harness and image
enablement, provider and GitHub reachability, and counts of workers, tasks
and wakes: no path, no fingerprint, no auth file name, no expiry time), and
report to the operator that
a harness is disabled, a credential is invalid or unverified, or a provider
is unavailable. It never calls the mutation endpoints.
