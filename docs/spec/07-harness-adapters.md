# 07. Harness adapter contracts

A harness adapter turns (attempt, identity bundle, credentials spec) into a
launch specification the execution provider can run, and turns the finished
run back into a parsed report. Adapters contain no lifecycle logic.

## Interface (`crucible/ports/harness.py`)

```python
class HarnessAdapter(Protocol):
    name: HarnessName                     # "claude_code" | "codex" | "agy" | "script-harness" (e2e only, 18)
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
`auth_failure`, `quota_exhausted`, `timeout`, `killed`, `crashed`, `lost`,
`unknown`. Which classes may retry is a policy decision (05b
`retry.eligible_classes`), narrowed by the contract's `retry_on`; a retry
happens only when the class is in both. Gate failures are never a class and
never retry.

## Common rules

- Non-interactive only. No TTY. Permission prompts disabled by the harness's
  own flag; the container is the boundary (12).
- Auth failures are classified from both stdout and stderr tails (S5).
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
  launch-time refusal with a wake, not a warning. Harness CLIs never
  self-update inside a worker: each image sets the CLI's auto-update
  opt-out and the root filesystem is read-only (13, S11).
- Retries and corrections keep the image digest of the task's first
  attempt unless the correction contract names a different image.
- No `gh` in worker images and no GitHub credential: adapters never
  instruct a worker to push or open a PR.

## Claude Code

- Launch: `claude -p --permission-mode bypassPermissions
  --append-system-prompt-file /crucible/identity/IDENTITY.md
  --output-format stream-json --verbose --model <model>` with the prompt on
  stdin: "Read /crucible/identity/IDENTITY.md and execute the task."
  (`stream-json` requires `--verbose` in print mode.)
- Credentials: subscription OAuth state. Two files are the credential: the
  credentials file inside the CLI's config directory and the top-level
  state file the CLI keeps beside it in the home directory. Both are
  `rw-narrow` (12) because the CLI refreshes tokens in place; the rest of
  the config directory (settings, hooks, MCP definitions) is mounted
  read-only from a Crucible-owned template. If the operator uses the CLI's
  long-lived token command instead, that token is delivered through the
  CLI's documented environment variable from the mounted file at container
  start, as the one exception to file-only delivery, and S1 records which
  path is in use.
- Shim: none if the checkout has a `CLAUDE.md`; otherwise a one-line
  untracked `CLAUDE.md` pointing at the identity file.
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
- Output: run with `--json` and `-o /crucible/report/codex-last-message.md`;
  the JSON event stream is stored as the transcript artifact, the last
  message as a summary artifact; the report file is the fact.

## AGY

- Launch: `agy -p "<pointer>" --model <model> --effort <effort>
  --dangerously-skip-permissions --add-dir /crucible/identity
  --output-format stream-json`, as observed in the CLI's own `--help` on the
  reference install; S3 confirmed the flags and stream-json output. The
  argv ceiling is the kernel's 128 KiB per argument (S3), so the prompt
  is under 1 KB by construction and the bundle travels by `--add-dir`.
- Credentials: `credential:agy` mounted to the Gemini config dir.
- Shim: untracked `AGENTS.md` if absent (AGY reads `AGENTS.md`; it does not
  read `GEMINI.md` reliably in headless mode per the operator's setup notes).
- Output: stream-json parsed like Claude Code's.

## Report parsing (all harnesses)

`/crucible/report/report.yaml` is parsed against `CompletionClaimV1` from
the collector's copy (08). Missing file with exit 0 is
`completed_without_report`, a hard failure of the report gate, never a
success. `blocked.md` with exit 75 produces an escalation and moves the task
to `blocked`; exit 75 without it is `failed`. Progress lines are ingested as events with the
worker as source and marked `unverified`.

## Local model endpoints

Codex and AGY can be pointed at an OpenAI-compatible endpoint by
configuration, which is how local models on the operator's DGX Spark and
RTX 9060 enter the routing policy with cost class none. The adapter gains
an `endpoint` in its launch context (`subscription` or a local URL on the
egress allowlist); the identity, report, and gate contracts do not change.
Which harness fronts each local server, what the model ids are, and how
quality compares are answered by spike S13 before the entries are enabled.
