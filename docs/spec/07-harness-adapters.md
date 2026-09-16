# 07. Harness adapter contracts

A harness adapter turns (attempt, identity bundle, credentials spec) into a
launch specification the execution provider can run, and turns the finished
run back into a parsed report. Adapters contain no lifecycle logic.

## Interface (`crucible/ports/harness.py`)

```python
class HarnessAdapter(Protocol):
    name: HarnessName                     # "claude_code" | "codex" | "agy"
    def capabilities(self) -> HarnessCapabilities: ...
    def credential_spec(self) -> CredentialSpec: ...
    def build_launch(self, ctx: LaunchContext) -> LaunchSpec: ...
    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport: ...
    def classify_exit(self, exit: ExitInfo, stderr_tail: str) -> ExitClass: ...
```

`LaunchSpec`: image, command and args, stdin bytes or file, env (no secret
values, only names that the provider resolves from mounts), mounts, working
dir, user, resource limits, network mode, expected exit semantics.

`ExitClass`: `completed`, `blocked` (75), `environment` (70),
`auth_failure`, `quota_exhausted`, `timeout`, `killed`, `crashed`,
`unknown`. Only `environment`, `auth_failure`, and `quota_exhausted` are
retry-eligible under the default policy, because those are not the worker's
fault and a retry is not a second guess at the work.

## Common rules

- Non-interactive only. No TTY. Permission prompts disabled by the harness's
  own flag; the container is the boundary (12).
- The task contract and identity are delivered as files. Argv carries only
  a short pointer. This is forced by AGY's 128 KB argv ceiling and applied
  uniformly.
- Working directory is the checkout. Home is a per-attempt scratch dir
  (`/home/worker`), not a host home.
- stdout and stderr are captured by the provider, chunked, and stored (10).
- Version pinning: each adapter declares the harness version range it was
  tested with; the worker image tag encodes the installed version;
  `GET /harnesses` reports both. Mismatch is a launch-time failure, not a
  warning.

## Claude Code

- Launch: `claude -p --permission-mode bypassPermissions
  --append-system-prompt-file /crucible/identity/IDENTITY.md
  --output-format stream-json --model <model>` with the prompt on stdin:
  "Read /crucible/identity/IDENTITY.md and execute the task."
- Credentials: subscription OAuth state under the harness config dir. Mounted
  from `credential:claude_code` to `/home/worker/.claude` as a narrow
  writable volume, because the CLI refreshes tokens in place. Spike S1 (21)
  must confirm refresh behavior and whether read-only suffices.
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
  the container is the boundary regardless. Where user namespaces are
  available in the container, spike S2 tests enabling Codex's sandbox as
  defense in depth.
- Credentials: `credential:codex` mounted to `/home/worker/.codex` (auth
  file). Narrow writable if refresh writes; spike S1 decides.
- Shim: untracked `AGENTS.md` if absent.
- Output: plain text stream; the last message is stored as the summary
  artifact; the report file is the fact.

## AGY

- Launch: `agy -p "<pointer>" --model <model> --effort <effort>
  --dangerously-skip-permissions --add-dir /crucible/identity
  --output-format stream-json`. Prompt is under 1 KB by construction.
- Credentials: `credential:agy` mounted to the Gemini config dir.
- Shim: untracked `AGENTS.md` if absent (AGY reads `AGENTS.md`; it does not
  read `GEMINI.md` reliably in headless mode per the operator's setup notes).
- Output: stream-json parsed like Claude Code's.

## Report parsing (all harnesses)

`report/report.yaml` is parsed against `CompletionClaimV1`. Missing file with
exit 0 is `completed_without_report`, a hard failure of the report gate, never
a success. `report/blocked.md` with exit 75 produces an escalation and moves
the task to `blocked`. Progress lines are ingested as events with the
worker as source and marked `unverified`.
