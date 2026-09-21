# C7a: administrative UI

## Result

Crucible serves a server-rendered administration interface at `/ui`. It uses
the existing application services, database, login registry, provider, and
audit stream. It adds no state store and no authorization role.

The two Lattice files are vendored from commit
`990071e5ef43adfd49b3bcc55b262ba9b3111cff`. Templates load `tokens.css`
before `lattice.css`, set `data-theme="dark"` on the root element, use no CDN,
and require no asset build.

`jinja2` is the template engine. `itsdangerous` signs the session document.
The signed document carries the bearer token and an independent CSRF value in
an HttpOnly, SameSite=Strict cookie. The database remains the authority for the
principal on every request, so revocation ends an existing UI session on its
next request. The signing key is process-local by design. A service restart
signs everyone out without changing or copying any bearer token.

## First run

The migration command checks for any enabled administrator after applying the
schema. If none exists, it creates `first-run-admin`, stores only the salted
token hash, and prints the token once inside a framed migration log block. A
later migration run prints nothing while an enabled administrator remains. If
all administrators are later revoked, migration creates a uniquely named
recovery administrator and prints that new token once. This was chosen over a
one-time file because Compose already gives the migration process an isolated, finite log,
`docker compose logs migrate` works without a host mount or file ownership
step, and no plaintext credential remains in a volume after the operator has
stored it. The sign-in page names that exact retrieval command.

## Harness login isolation

Normal deployment login runs the command from the harness's promoted worker
image. Its container receives one writable subpath from the Crucible-owned
credential volume, no workspace, the internal worker network, and the same
egress proxy allowlist as that adapter. Root is read-only, capabilities are
dropped, and resource limits apply. The TTY is attached directly while the
Docker log driver is disabled, so a one-time Claude Code token can be captured
to `oauth-token` without entering container logs. Completion, cancellation,
timeout, and startup failure all reap the container. Hermes has no login and
says so on its page. Local CLI login remains available on a host that carries
the harness executable.

## Pages

| Page | State and operations |
|---|---|
| Status | Supervisor, providers, task counts, wakes, and plain readiness actions |
| Harnesses | Supported and installed versions, images, both enable gates, enable and disable |
| Credentials | Sanitized state, validate, probe, login, cancel, finish, and remove |
| Images | Provider image list and promotion |
| Routing | Delivery policy, routing policy, pools, exhaustion marks, upload, and clear |
| Repositories | List, register or update, and remove when unreferenced |
| Tokens | Principal list, one-time creation, and revocation |
| GitHub | App presence, repository connectivity, and check |
| Workers | Active attempts and stored live log tail |
| Tasks | Failed and blocked task lists and lifecycle counts |
| Wakes | Aggregate pending state and the signed-in principal's records |
| Retention | Last sweep summary and recent cleanup actions |
| Audit | Cursor-paged administrative events |
| Bootstrap | List, show, and commit verified imports |
| Settings | Every process setting, effective source, and restart-bound reason |

Scripts are not required for any page except login polling. Without script,
the same page provides a refresh link and every form remains functional.

## Settings disposition

`editable` means the full validated policy document is editable from Routing.
`read-only` means it is process configuration read at startup. `hidden` is
used only for plaintext secret values, whose presence and source remain shown.

| Settings fields | Disposition | Reason or surface |
|---|---|---|
| `service.bind`, `service.render_timezone`, `service.artifact_root`, `service.log_level` | read-only | Bind, rendering, storage, and logging are established at process start. |
| `database.url` | hidden value, presence read-only | Connection authority is established at startup and the URL may contain a password. |
| `supervisor.tick_seconds`, `supervisor.lease_ttl_seconds`, `supervisor.reconcile_interval_seconds`, `supervisor.attempt_lease_ttl_seconds`, `supervisor.checkout_lease_ttl_seconds`, `supervisor.grace_seconds`, `supervisor.holder` | read-only | Lease and loop timing are fixed for the running supervisor. |
| `docker.enabled`, `docker.host`, `docker.api_timeout_seconds`, `docker.mount_kind`, `docker.artifact_volume`, `docker.artifact_host_root`, `docker.credential_root`, `docker.credential_host_root`, `docker.credential_volume`, `docker.workers_network` | read-only | Provider authority, daemon paths, and mount topology are startup boundaries. |
| `docker.egress_proxy`, `docker.egress_allowlist`, `docker.no_proxy` | read-only | The proxy process and its generated ACL must change with a restart, not drift from a live form edit. Runtime task egress knobs remain policy fields below. |
| `docker.collector_timeout_seconds`, `docker.verifier_timeout_seconds`, `docker.report_size_cap_bytes`, `docker.workspace_dir_mode`, `docker.use_reference_cache`, `docker.max_concurrency`, `docker.extra_image_allowlist` | read-only | These shape provider construction and resource admission at startup. |
| `github.enabled`, `github.api_base`, `github.api_timeout_seconds`, `github.poll_interval_seconds`, `github.reactions_poll_interval_seconds`, `github.webhook_enabled`, `github.publisher_image`, `github.publisher_network`, `github.publisher_egress_proxy`, `github.publisher_timeout_seconds`, `github.credential_host`, `github.allow_issue_comments`, `github.ci_log_excerpt_bytes` | read-only | The client, poller, publisher boundary, and webhook route are wired at startup. |
| `github.app.app_id` | read-only | Public identifier, shown as a value and used when the GitHub client is constructed. |
| `github.app.private_key_path`, `github.app.webhook_secret_path` | read-only | Paths are shown; file contents are never configuration and never displayed. |
| `wake.webhook_url`, `wake.secret` | hidden value, presence read-only | Both may carry secret material and construct the wake deliverer at startup. |
| `wake.timeout_seconds` | read-only | The delivery client is constructed with this timeout. |
| `credentials.<harness>.source`, `credentials.<harness>.path`, `credentials.<harness>.mount_mode` | read-only | Paths and minimum mount behavior define the credential isolation boundary. Credential state is managed on Credentials. |
| `harnesses.<harness>.enabled`, `harnesses.<harness>.reason` | read-only | This is the restart-bound configuration gate. The independent runtime gate is editable on Harnesses. |
| `admin.credential_retention_hours`, `admin.probe_timeout_seconds`, `admin.login_timeout_seconds`, `admin.login_commands` | read-only | Administrative workers and test overrides are wired at startup. Command values are not shown as secret values, but production defaults need no override. |
| `spark_endpoint_url` | read-only | The local model endpoint is validated and wired at startup. |

## Policy disposition

All policy knobs below are editable from the Routing page by uploading a new,
validated, immutable version. Referenced versions cannot be overwritten. The
three operator-only relaxations retain their existing role checks.

| Policy fields | Disposition |
|---|---|
| `schema_version`, `name`, `version`, `description` | editable |
| `limits.timeout_seconds.min`, `.max`, `.default`, `limits.max_attempts.max`, `.default`, `limits.grace_seconds`, `limits.stall_warn_seconds`, `limits.stall_fail_seconds`, `limits.auth_retry_delay_seconds`, `limits.escalation_stale_hours`, `limits.wake_retry_hours` | editable |
| `retry.eligible_classes`, `retry.auth_failure_max` | editable |
| `concurrency.per_provider`, `concurrency.per_harness.<harness>` | editable |
| `resources.cpus`, `resources.memory`, `resources.pids`, `resources.tmpfs_total` | editable |
| `network.mode`, `network.egress_allowlist`, `network.harness_endpoints` | editable |
| `routing.policy.name`, `routing.policy.version` | editable |
| `images.allowlist`, `images.require_default_or_retained` | editable |
| `git.author_name`, `git.author_email`, `git.commit_trailer`, `git.work_branch_pattern`, `git.protected_branches` | editable |
| `repository.required_checks` | editable |
| `gates.pre_pr`, `gates.publication`, `gates.post_pr`, `gates.skipped` | editable |
| `deliverables.allow_branch_only`, `deliverables.on_out_of_band_head` | editable, first field operator-only when relaxed |
| `pull_request.require_pre_pr_verification`, `pull_request.open_only_after_pre_pr_gates_pass`, `pull_request.publish_requires_acceptance`, `pull_request.title_from`, `pull_request.body_template`, `pull_request.closing_refs` | editable |
| `internal_review.required`, `internal_review.required_for_corrections`, `internal_review.reviewer_must_not_be_author`, `internal_review.executor` | editable |
| `external_review.provider`, `external_review.reviewer_logins`, `external_review.required_rounds`, `external_review.retrigger_after_correction`, `external_review.require_review_on_final_sha`, `external_review.require_feedback_disposition`, `external_review.accepted_signals`, `external_review.components`, `external_review.round_counting`, `external_review.wait_timeout_hours` | editable |
| `ci_certification.require_green_on_final_sha`, `ci_certification.required_checks`, `ci_certification.allow_no_ci`, `ci_certification.on_failure`, `ci_certification.automatic_retry`, `ci_certification.automatic_worker_correction`, `ci_certification.wait_timeout_hours` | editable, `allow_no_ci` operator-only when true |
| `release.require_operator_approval`, `release.authorization_recorder`, `release.trigger`, `release.tag_pattern`, `release.version_files`, `release.changelog_required` | editable, approval may only be relaxed by operator or admin |
| `cleanup.workspace_on_success`, `cleanup.workspace_on_failure`, `cleanup.container_remove`, `cleanup.credential_volume_remove` | editable |
| `retention.logs_and_transcripts_days`, `retention.bootstrap_archive_days`, `retention.completed_workspaces_days`, `retention.wakes_after_ack_days`, `retention.indefinite` | editable |
| `RoutingPolicyV1.schema_version`, `.name`, `.version` | editable |
| `tiers.<tier>.allowed_capability`, `tiers.<tier>.prefer` | editable |
| `models[].id`, `.harness`, `.endpoint`, `.endpoint_url`, `.capability`, `.cost`, `.speed`, `.pool`, `.weight`, `.enabled`, `.disabled_reason` | editable |
| `pools.<pool>.window`, `.budget_units`, `.soft_limit`, `.default_cooldown_seconds`, `.max_concurrency` | editable |
| `rotation.strategy`, `rotation.quality_feedback`, `rotation.quality_window` | editable |
| `reroute.reroute_max`, `reroute.resume_max_wait_seconds` | editable |

No settings field or policy knob is omitted. Plaintext bearer tokens,
credential files, GitHub keys, webhook secrets, and device codes are not
settings and are never rendered.

## Live compose evidence

The branch image was built and started as the isolated `crucible-c7a` Compose
project. A fresh database created `first-run-admin`; the migration log showed
the framed token once, and the browser used it to reach every page. A second
migration emitted no token. `make smoke` then submitted task
`01M313XZ02ST8CK0PKQMK2YVFD`, ran it with the fake provider, recorded its
non-author review and acceptance, and finished successfully. The workers page
was also viewed while a separate fake worker was running so the active row and
local-time timestamps were visible. A final rebuild and smoke run on the same
stack accepted task `01M316622MKWNA2K5JQ04QEBFZ`.

After the adversarial-review fixes, a second fresh project,
`crucible-c7a-final`, started on a new database. Its smoke walk fetched the
signed pre-authentication nonce, signed in with the one-time migration token,
walked every page, and accepted task `01M3193VMG9NJ8J5Y2VWRQJ7V0`.

A real Codex login was started from the page with the promoted pinned worker
image. The page reached `WAITING_FOR_OPERATOR` and displayed the provider URL
and one-time code. The committed screenshot replaces both with explanatory
text. The run was cancelled from the same form without selecting replace, and
the login container was reaped. Earlier live iterations exposed three defects
that are fixed in this branch: a fresh named credential volume was owned by
root, the upgraded HTTP response was not retained for the attached TTY's
lifetime, and task retention treated the administrative login label as an
orphan task attempt. The Compose initializer changes only the credential
volume root, the attach owner retains the response, and retention now excludes
login ids owned by the running provider while reaping stale login labels after
a restart. The container also invokes
the adapter's declared login executable directly, without inheriting a worker
entrypoint that may require a task identity bundle.

The dedicated admin tier could not validate the real AGY credential on this
workstation because `/var/lib/crucible/credentials/agy/oauth-token` is absent.
The run completed with one passing isolation case and two failures: the AGY
credential check, followed by the dependent probe. The in-service completed
login path is covered by the stub CLI integration and Docker e2e tests instead.

## Screenshots

All images below were opened before commit. The login-in-progress image was
redacted in the browser DOM before capture, and no provider URL or one-time
code is stored in the file.

| Page | Screenshot |
|---|---|
| Sign in | `docs/implementation-notes/c7a/sign-in.png` |
| Status | `docs/implementation-notes/c7a/status.png` |
| Harnesses | `docs/implementation-notes/c7a/harnesses.png` |
| Credentials | `docs/implementation-notes/c7a/credentials.png` |
| Codex login | `docs/implementation-notes/c7a/codex-login.png` |
| Hermes login | `docs/implementation-notes/c7a/hermes-login.png` |
| Login in progress | `docs/implementation-notes/c7a/login-in-progress.png` |
| Images | `docs/implementation-notes/c7a/images.png` |
| Routing | `docs/implementation-notes/c7a/routing.png` |
| Repositories | `docs/implementation-notes/c7a/repositories.png` |
| Tokens | `docs/implementation-notes/c7a/tokens.png` |
| GitHub | `docs/implementation-notes/c7a/github.png` |
| Workers | `docs/implementation-notes/c7a/workers.png` |
| Tasks | `docs/implementation-notes/c7a/tasks.png` |
| Wakes | `docs/implementation-notes/c7a/wakes.png` |
| Retention | `docs/implementation-notes/c7a/retention.png` |
| Audit | `docs/implementation-notes/c7a/audit.png` |
| Bootstrap | `docs/implementation-notes/c7a/bootstrap.png` |
| Settings | `docs/implementation-notes/c7a/settings.png` |

## Review findings

The single required non-author adversarial review found six issues. All were
addressed before the pull request, and the contract forbids a second round.

| Severity | Finding | Disposition |
|---|---|---|
| high | Replacement could retire a valid credential before a missing image or replacement-directory failure. | Image selection now precedes retirement; every fallible post-retirement step is inside the restore boundary. Integration tests cover missing promotion and `mkdir` failure. |
| high | A service crash could leave a login container that task retention skipped forever. | The provider tracks login ids owned by the current process. Retention preserves those and reaps login labels left by a prior process. A unit test models restart recovery. |
| high | Revoking the fixed first-run administrator prevented later migration recovery. | Migration keeps the stable first name on a fresh database and creates a unique recovery administrator if that name already exists but no enabled administrator remains. The integration test authenticates the recovery token. |
| medium | The sign-in POST had no pre-authentication CSRF nonce. | The GET now sets a signed, HttpOnly, SameSite=Strict, ten-minute pre-authentication cookie and matching hidden nonce. Missing or mismatched values return 403 before token authentication. |
| medium | UI mutation parity coverage did not enumerate every dispatch. | The integration matrix now drives every remaining UI mutation and proves it reaches the same application-service entry point used by API and CLI coverage. Incorrect readiness test names were corrected. |
| low | Reader principals could see login operation forms. | Reader rendering now shows only the login state and an administrator-required notice, with no URL, code, output, form, or polling script. |
