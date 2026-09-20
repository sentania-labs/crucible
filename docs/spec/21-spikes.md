# 21. Technical spikes (Phase C0)

Each spike is a bounded task with a written result. None produce product
code. A spike that fails changes the design in this specification before
C1 starts. S9 and S10 run first.

| ID | Question | Method | Pass condition |
|---|---|---|---|
| S9 | Does a rootless Docker daemon dedicated to Crucible run the worker images, the socket proxy, and the egress proxy with acceptable performance on the development workstation? | Install rootless dockerd for a service user; run the hardened worker shape, an internal network, and a CONNECT proxy against it; measure build and start times | Documented pass (becomes the default local arrangement) or the specific blocker (fallback to the host socket, risk recorded) |
| S10 | Can Crucible mint a repository-scoped installation token from a mounted App private key and push a branch and an annotated tag from a publisher container without the token touching env, logs, or disk outside tmpfs? | Create a throwaway App and repository; sign the JWT; exchange; push through a credential helper reading a tmpfs file; grep the container and host for the token afterward | Push and tag succeed; token found nowhere but the tmpfs file; App permission set confirmed minimal |
| S1 | Does each harness's subscription auth work from a mounted config directory inside a non-root container, and does it write back on refresh? | Build the three base images; mount a copy of each host credential directory ro, run a trivial prompt; repeat rw-narrow; inspect the directory after; repeat after forcing token expiry where possible; run two concurrent workers of one harness | Each harness completes a prompt with no commercial API key; refresh behavior documented; concurrency outcome documented |
| S2 | Can Codex's own sandbox run inside the container as defense in depth? | Run `codex exec --sandbox workspace-write` in the container with and without user namespaces enabled | Documented either way; the primary boundary does not depend on it |
| S3 | Does AGY operate headless with `--add-dir` and a pointer prompt, reading `AGENTS.md`, and what is its real argv ceiling? | Run with a 100 KB identity bundle mounted and a short prompt | Task completes; ceiling recorded |
| S4 | With harness sandboxes disabled, does the container hardening hold? | From inside a worker: attempt socket access, proxy access, database access, other credential mount, a `git push`, capability use, privilege escalation, write outside mounts, outbound to a non-allowlisted host | Every attempt fails; results recorded |
| S5 | Is exit-code and report-file detection reliable across the three harnesses? | Force 0, 75, 70, crash, hang, SIGTERM in each | Adapter `classify_exit` table confirmed |
| S6 | Which egress endpoints does each harness need? | Run with `network: none`, then with a logging allowlist proxy; collect hostnames | Allowlist per harness recorded |
| S7 | Can harness versions be pinned and updated predictably in images? | Build images at two versions; verify version labels and reporting; rebuild reproducibly | Same inputs produce the same image digest, or the reason is recorded |
| S8 | Does a running worker survive a Crucible container restart and a Compose restart? | Start a long fake task; restart `crucible`; then `docker compose restart` | Worker unaffected; re-attach succeeds; logs resume |
| S11 | Can each harness be prevented from self-updating inside the container, and does it report its version reliably? | Set each CLI's documented auto-update opt-out; run with a read-only root; observe update attempts and `--version` output | No update occurs; version reported matches the image label; opt-out mechanism recorded per harness |
| S12 | What does the configured external reviewer actually emit on a PR, under which login, and how does it get triggered? | Open a PR on the throwaway repository with the reviewer App installed; capture the webhook deliveries and API objects | Login, signal kind, reviewed-SHA field, and trigger mechanism recorded; `reviewer_logins` default confirmed |
| S1b | Does establishing and using Crucible's dedicated Claude Code, Codex, and AGY credential sessions leave the operator's normal interactive sessions valid, including across a Crucible-side refresh? | Per harness: confirm the operator's session works; log in directly into Crucible's dedicated directory; run a trivial prompt with it; confirm the operator's session; cause or observe a Crucible refresh where practical; confirm the operator's session again. Record only success or failure, timestamps, harness versions, auth-file hashes, and refresh behavior; never token values; never copy credentials | Pass per harness, or stop and escalate on the first invalidation; no retries, no copying. Blocking prerequisite for enabling that harness in C5 |
| S13 | Can Codex or AGY run a task against a local OpenAI-compatible model server (RTX 9060 now, DGX Spark when it arrives) inside the worker container with the same identity, report, and gate contracts, and how do quality, speed, and cost compare on a fixed task set? | Stand up the local server on the host, allowlist it in the egress proxy, run three fixed tasks per model, collect AttemptMetrics | Documented per model; routing policy entries enabled or left disabled with the reason |
| S14 | Can Crucible observe current subscription quota state per harness, and use it for routing and for a wait-and-resume strategy? | Evaluate the third-party reader `quota-axi`, pinned and installed locally, pointed only at Crucible's dedicated credential directories through each harness's own configuration or home variable; record what it reports per harness, what it reads to answer, and whether answering costs provider quota; evidence that it never reads the operator's daily-use directories | Per-harness field shapes and reset availability recorded from real redacted output; isolation from the operator's directories evidenced, or the spike stops and reports; a decision of proceed, adjust design, or escalate, naming whether to take the tool or build our own, with a proposed ADR |
| S16 | Which harness should front `gpt-oss:120b` on the DGX Spark: Codex, Claude Code, or Hermes? | Run one fixed three-task suite on the host and in worker-shaped containers through each qualifying harness, using only the configured Spark endpoint and model; collect mechanical outcomes, timing, token and tool-call metrics, malformed calls, exits, and transcript fit | One evidence-backed harness recommendation, exact adapter and routing-policy changes, local concurrency and egress requirements, and reasons the other candidates are not selected ([result](../spikes/S16.md)) |

S1b status, as the C5a live runs left it: Claude Code and AGY are
**verified** for daily-session compatibility and enabled; Codex is not.
AGY's step 5, a Crucible-side refresh, was observed on 2026-09-17 at 00:50
CDT when the first run past the token's one-hour expiry rotated it, and
step 6, the operator's own session answering and refreshing normally
afterwards, was confirmed at 06:32 CDT. Codex's step 5 is still pending:
its auth file was unchanged on every C5a run, the token still valid from
the login, so no Crucible-side refresh has been observed and Codex ships
disabled in both gates (25) until one is.

Spike results go in `docs/spikes/S<n>.md` with commands, output excerpts,
and the decision. Credentials used during spikes are the operator's own
host directories, copied to a scratch location and deleted after; nothing
is committed. Throwaway Apps and repositories created for S10 and S12 are
deleted after, with the deletion recorded.
