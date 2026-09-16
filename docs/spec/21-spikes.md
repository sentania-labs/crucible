# 21. Technical spikes (Phase C0)

Each spike is a bounded task with a written result. None produce product
code. A spike that fails changes the design in this specification before
C1 starts.

| ID | Question | Method | Pass condition |
|---|---|---|---|
| S1 | Does each harness's subscription auth work from a mounted config directory inside a non-root container, and does it write back on refresh? | Build the three base images; mount a copy of each host credential directory ro, run a trivial prompt; repeat rw-narrow; inspect the directory after; repeat after forcing token expiry where possible; run two concurrent workers of one harness | Each harness completes a prompt with no commercial API key; refresh behavior documented; concurrency outcome documented |
| S2 | Can Codex's own sandbox run inside the container as defense in depth? | Run `codex exec --sandbox workspace-write` in the container with and without user namespaces enabled | Documented either way; the primary boundary does not depend on it |
| S3 | Does AGY operate headless with `--add-dir` and a pointer prompt, reading `AGENTS.md`, and what is its real argv ceiling? | Run with a 100 KB identity bundle mounted and a short prompt | Task completes; ceiling recorded |
| S4 | With harness sandboxes disabled, does the container hardening hold? | From inside a worker: attempt socket access, proxy access, database access, other credential mount, capability use, privilege escalation, write outside mounts, outbound to a non-allowlisted host | Every attempt fails; results recorded |
| S5 | Is exit-code and report-file detection reliable across the three harnesses? | Force 0, 75, 70, crash, hang, SIGTERM in each | Adapter `classify_exit` table confirmed |
| S6 | Which egress endpoints does each harness need? | Run with `network: none`, then with a logging allowlist proxy; collect hostnames | Allowlist per harness recorded |
| S7 | Can harness versions be pinned and updated predictably in images? | Build images at two versions; verify version reporting; rebuild reproducibly | Same inputs produce the same image digest, or the reason is recorded |
| S8 | Does a running worker survive a Crucible container restart and a Compose restart? | Start a long fake task; restart `crucible`; then `docker compose restart` | Worker unaffected; re-attach succeeds; logs resume |

| S9 | Does a rootless Docker daemon dedicated to Crucible run the worker images and the socket proxy with acceptable performance? | Install rootless dockerd for a service user; run the C3 e2e tier against it | Documented pass or the specific blocker |

Spike results go in `docs/spikes/S<n>.md` with commands, output excerpts,
and the decision. Credentials used during spikes are the operator's own
host directories, copied to a scratch location and deleted after; nothing
is committed.
