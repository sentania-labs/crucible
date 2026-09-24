# 07. Harness adapter contracts

A harness adapter turns (attempt, identity bundle, credentials spec) into a
launch specification the execution provider can run, and turns the finished
run back into a parsed report. Adapters contain no lifecycle logic.

## Interface (`crucible/ports/harness.py`)

```python
class HarnessAdapter(Protocol):
    name: HarnessName                     # "claude_code" | "codex" | "agy" | "hermes" | "script-harness" (e2e only, 18)
    supported_versions: VersionRange      # tested range; launch refused outside it
    def capabilities(self) -> HarnessCapabilities: ...
    def credential_spec(self) -> CredentialSpec: ...
    def build_launch(self, ctx: LaunchContext) -> LaunchSpec: ...
    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport: ...
    def classify_exit(self, exit: ExitInfo, stdout_tail: str, stderr_tail: str) -> ExitClass: ...
    # both tails: Claude Code and AGY report a missing or expired login on stdout (S5)
```

`LaunchSpec`: image, command and args, stdin bytes or file, env (no secret
values, only names that the provider resolves from mounts), mounts, working
dir, user, resource limits, network mode, expected exit semantics.

`ExitClass` is the one enum used by contracts, policies, and 16:
`completed`, `completed_without_report`, `blocked`, `environment`,
`auth_failure`, `provider_error`, `quota_exhausted`, `timeout`, `killed`, `crashed`, `lost`,
`unknown`. Which classes may retry is a policy decision (05b
`retry.eligible_classes`), narrowed by the contract's `retry_on`; a retry
happens only when the class is in both. Gate failures are never a class and
never retry.

## Common rules

- Non-interactive only. No TTY. Permission prompts disabled by the harness's
  own flag; the container is the boundary (12).
- Auth failures are classified from both stdout and stderr tails (S5),
  and only on a non-zero exit: the auth and quota patterns never turn a
  successful run into a failure, and never reclassify a termination
  Crucible performed.
- Codex is launched with `--disable plugins` so it does not contact
  github.com or chatgpt.com for plugin sync (S6).
- The task contract and identity are delivered as files. Argv carries only
  a short pointer. This is forced by AGY's 128 KB argv ceiling and applied
  uniformly.
- Working directory is the checkout. Home is a per-attempt scratch dir
  (`/home/worker`), not a host home.
- stdout and stderr are captured by the provider, chunked, and stored (10).
- Version pinning: each adapter declares the harness version range it was
  tested with; the worker image carries the installed version in a label;
  the attempt records the image digest it ran; `GET /harnesses` reports
  installed and supported versions. A combination outside the range is a
  launch-time refusal with a wake, not a warning. The image must also
  carry a `crucible.harness` label equal to the harness the execution
  asks for; an image that names no harness, or names a different one, is
  refused before anything is seeded, whatever its version label says. An
  image is never launched with a credential on the strength of a version
  label alone. Harness CLIs never
  self-update inside a worker: each image sets the CLI's auto-update
  opt-out and the root filesystem is read-only (13, S11).
- Retries and corrections keep the image digest of the task's first
  attempt unless the correction contract names a different image.
- No `gh` in worker images and no GitHub credential: adapters never
  instruct a worker to push or open a PR.

## Claude Code

- Version floor: `>=2.1.277,<2.2.0`. Claude Code 2.1.277 is the first release
  whose changelog says it reads `AGENTS.md` under the default
  `instructionFiles` setting. Older worker images are refused at launch.

- Launch: `claude -p --permission-mode bypassPermissions
  --append-system-prompt-file /crucible/identity/IDENTITY.md
  --output-format stream-json --verbose --model <model>` with the prompt on
  stdin: "Read /crucible/identity/IDENTITY.md and execute the task."
  (`stream-json` requires `--verbose` in print mode.)
- Credentials: subscription OAuth state. Crucible's dedicated session uses
  the CLI's long-lived token (S1b), kept as the file `oauth-token` in the
  credential directory, with the top-level state file `.claude.json`
  seeded beside it so the CLI finds the state it expects, and
  `CLAUDE_CONFIG_DIR` pointed at the mounted copy so both live in one
  directory. The token reaches the CLI through its documented environment
  variable, read from the mounted file at container start, as the one
  exception to file-only delivery. Neither file is written back: the
  long-lived token does not refresh, and `.claude.json` is state the CLI
  rewrites on every run, not a credential. The mount is `rw-narrow` (12)
  because the CLI writes that state in place. The rest of the config
  directory (settings, hooks, MCP definitions) is mounted read-only from a
  Crucible-owned template.
- Endpoints: `api.anthropic.com` (plus `mcp-proxy.anthropic.com` only if
  account MCP connectors are wanted). Confirmed by a task completed
  through the filtering proxy, so the list is no longer provisional.
- Login endpoints (the Kubernetes login Job's whole egress, 26):
  `platform.claude.com`, where `setup-token` exchanges the pasted code. Read
  from the pinned binary's strings on 2026-09-24, not yet observed on a live
  login.
- Shim: one-line untracked `AGENTS.md` pointing at the identity file when the
  checkout has no `AGENTS.md`. Claude Code alone suppresses that shim when the
  checkout has its own `CLAUDE.md`, because it reads that file when it wins
  under the default `instructionFiles` setting, and records that the project's
  `CLAUDE.md` is in force. Codex and AGY do not read `CLAUDE.md`, so it does not
  suppress their shim. A committed `CLAUDE.md` and a committed `AGENTS.md`
  remain project files and no generated shim is written.
- Stream-json lines are parsed into progress events (tool use, text) at low
  fidelity; the full stream is stored as the transcript artifact.
- Known: nested invocation from inside another Claude session works, but
  workers never run inside a session anyway.

## Codex

- Launch: `codex exec --dangerously-bypass-approvals-and-sandbox
  --model <model> -C <checkout>` with `IDENTITY.md` followed by the prompt on
  stdin. `--sandbox read-only` and friends are not used: Codex's bubblewrap
  sandbox needs user namespaces, which the reference workstation denies, and
  the container is the boundary regardless. S2 showed Codex's sandbox
  cannot run inside the worker container at all (no bubblewrap in the
  image, and user namespaces are blocked under every seccomp profile
  tried), so it is never enabled; the container is the only boundary.
- Credentials: `credential:codex` is `rw-narrow` from the start: the CLI
  writes session and log state beside its auth file, so a read-only mount
  fails before auth is tested. Only the auth file syncs back (12).
- Shim: untracked `AGENTS.md` if absent.
- Endpoints: `api.openai.com`, `auth.openai.com`, and `chatgpt.com`.
  `chatgpt.com` is required, not conditional: with a ChatGPT-plan login
  (`auth_mode = chatgpt`) that host is the backend, and a run without it
  reconnects until it is permitted and never reaches the model.
  `ab.chatgpt.com` stays denied.
- Login endpoints: `auth.openai.com`, the device-code and token endpoints
  (the pinned binary's strings, 2026-09-24).
- The worker image carries the code-mode host companion binary the CLI
  spawns for the 5.6 model family; without it those models fail closed.
  It is an asset of the same pinned CLI release and is pinned by the
  tarball's checksum like the CLI itself (13).
- Output: run with `--json` and `-o /crucible/report/codex-last-message.md`;
  the JSON event stream is stored as the transcript artifact, the last
  message as a summary artifact; the report file is the fact.

## AGY

- Launch: `agy -p "<pointer>" --model <model> [--effort <effort>]
  --dangerously-skip-permissions --add-dir /crucible/identity
  --output-format stream-json --print-timeout <attempt timeout>`, as
  observed in the CLI's own `--help` on the
  reference install; S3 confirmed the flags and stream-json output. The
  argv ceiling is the kernel's 128 KiB per argument (S3), so the prompt
  is under 1 KB by construction and the bundle travels by `--add-dir`.
  `--print-timeout` defaults to five minutes in the CLI, which would cut a
  longer task, so it follows the attempt's own timeout. `--effort` is not
  a separate flag for every model: the Flash models carry the effort inside
  the model id (the low-effort Flash model is `gemini-3.8-flash-low`) and
  refuse the flag, so no effort is passed for them.
- Credentials: `credential:agy` mounted to the Gemini config dir,
  `rw-narrow` (12): the first Crucible-side run past the token's one-hour
  expiry refreshed it in place and the copy carried a newer expiry, so the
  token file syncs back by that field.
- Endpoints: `daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`,
  `www.googleapis.com`, and `lh3.googleusercontent.com`. The last two are
  the CLI's eligibility check, which calls the userinfo endpoint and then
  fetches the account's profile picture before any turn and fails closed
  when either is refused. Neither is a model endpoint; both are what the
  CLI needs. The list is no longer provisional.
- Login endpoints: `oauth2.googleapis.com` and `www.googleapis.com`, the
  code exchange and the userinfo call (the pinned binary's strings,
  2026-09-24). AGY's login command ends with a prompt to its model API, which
  a Kubernetes login Job cannot reach, so it exits non-zero there; the token
  it wrote is judged by its shape and then by the probe.
- Shim: untracked `AGENTS.md` if absent (AGY reads `AGENTS.md`; it does not
  read `GEMINI.md` reliably in headless mode per the operator's setup notes).
- Templates: none. AGY's config directory carries no settings file the
  adapter needs to pin, so nothing is mounted read-only on top of the copy
  and `config/` is not seeded.
- Output: stream-json parsed like Claude Code's, keyed by `event` rather
  than `type` (`{"event": "result", "result": {...}}`); the adapter reads
  both keys.

## Hermes

- Hermes 0.19.0 fronts an OpenAI-compatible local gateway. The routing model id is
  supplied by policy, with `coder` as the C10 migration entry. The adapter does not
  pin a model id. A local launch requires an `endpoint_url` ending in `/v1`.
- Its credential spec names only `api-key`, maps it to `OPENAI_API_KEY`, mounts the
  per-attempt copy read-only, and never syncs it back. When no credential source is
  configured the launch uses the literal non-secret placeholder `local-no-auth`, so an
  unauthenticated compatible endpoint remains possible.
- Launch is `crucible-hermes --ignore-user-config --ignore-rules --safe-mode
  --yolo --provider openai-api --model <routing-model> --toolsets terminal,file
  --usage-file /crucible/report/hermes-usage.json -z <pointer>`. `--yolo` is
  permitted only because the read-only worker container is the permission
  boundary. `HERMES_HOME` is a per-attempt tmpfs, so rules, memory, plugins, MCP
  configuration, and session `state.db` do not cross attempts.
- Hermes's usage JSON is mandatory run evidence. Its input and output token counts
  come from that file. The image wrapper enriches duration and tool-call count from
  the same attempt's SQLite session row before exit. Missing or unparsable usage
  fails `run_evidence_present`.
- Classification checks Crucible termination facts first, then usage and provider
  text, then report presence. `failed: true` overrides exit 0. Exit 75 is
  `provider_error` unless explicit quota text makes it `quota_exhausted`; a local
  5xx or connection refusal is always `provider_error` and never marks the pool.
- Crucible's launch wrapper is the sole transcript writer. The Hermes image wrapper
  inherits stdout and only enriches the usage record after the child exits.

## Report parsing (all harnesses)

`/crucible/report/report.yaml` is parsed against `CompletionClaimV1` from
the collector's copy (08). A missing file with exit 0 is
`completed_without_report`. A file that is present but does not parse is
recorded as `report_parse_failed` with the parser's errors and
`report_present` true; it is never recorded as "no report". Either way the
report gate fails hard and neither is a success; what differs is that the
record says which happened. `blocked.md` with exit 75 produces an escalation
and moves the task
to `blocked`; exit 75 without it is `failed`. Progress lines are ingested as
`worker_progress` events under the principal `worker` and marked
`unverified`, at most 200 per attempt and 1000 characters per line, each
line redacted before it is stored (12).

## Local model endpoints

`LaunchContext` and `LaunchSpec` carry `endpoint` as `subscription` or `local`
and a separate optional `endpoint_url`. Local requires the URL; subscription
forbids it. The supervisor preserves both fields through launch reconstruction.
Local routing does not imply credential absence. The provider mounts the selected
harness's declared credential when one is configured. Hermes therefore receives its
read-only `api-key` copy, while a deployment without that file gets the explicit
fallback. For an optional credential, a configured directory that does not hold its
required auth files (the empty directory Compose creates) counts as not mounted.
The identity, report, and gate contracts do not change.

Hermes is the local gateway front end. Other harness and local-server
combinations remain disabled until their own compatibility and quality evidence
exists.
