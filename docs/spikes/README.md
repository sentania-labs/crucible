# Spike results (Phase C0)

One file per spike from `docs/spec/21-spikes.md`, each with the commands
run, output excerpts, and a decision: proceed, adjust design, or escalate.
Nothing here is product code. A spike that fails changes the specification
before C1 starts.

| ID | Question | Result |
|---|---|---|
| S1 | Does each harness's subscription auth work from a mounted config directory inside a non-root container? | adjust design: all three complete a prompt as uid 1000 from a mounted copy; Claude Code `rw-narrow` shape confirmed; Codex and AGY refreshed their own tokens on the first run, which stopped their authenticated spikes and is escalated ([S1.md](S1.md)) |
| S2 | Can Codex's own sandbox run inside the container? | proceed, not enabled: no bubblewrap in the image or binary, and user namespaces are unavailable under the hardened shape on this host ([S2.md](S2.md)) |
| S3 | Does AGY operate headless with `--add-dir`, and what is its argv ceiling? | proceed, partial: headless flags and stream-json confirmed; the ceiling is the Linux 128 KiB per-argument limit; the 100 KB bundle run is blocked on the S1 finding ([S3.md](S3.md)) |
| S4 | With harness sandboxes disabled, does the container hardening hold? | proceed, partial: every probe failed on the rootful daemon, but the credential, database and proxy checks ran against an empty environment; complete on rootless in C3 with the real control plane and credentials present ([S4.md](S4.md)) |
| S5 | Is exit-code and report-file detection reliable? | proceed, with `--init` added to the worker launch so SIGTERM reaches a harness running as PID 1 ([S5.md](S5.md)) |
| S6 | Which egress endpoints does each harness need? | adjust design: all three allowlists provisional (Claude Code discovered live but never run behind the filter alone); complete with an authenticated run through the actual filter in C3; Codex gains `--disable plugins` ([S6.md](S6.md)) |
| S7 | Can harness versions be pinned and updated predictably in images? | proceed, partial: pinned and reproducible at one version each; the second-version update is not exercised until a new pin lands ([S7.md](S7.md)) |
| S8 | Does a running worker survive a supervisor restart? | proceed, partial: survives restart of a stand-in supervisor container with logs resumable by offset; Crucible itself and `docker compose restart` repeat in C3 ([S8.md](S8.md)) |
| S11 | Can each harness be prevented from self-updating, and does it report its version reliably? | proceed for Claude Code (confirmed in an authenticated run); partial for Codex and AGY (unauthenticated runs only); versions match labels ([S11.md](S11.md)) |

Worker base images used by the spikes are built from `images/` with
`images/build.sh`; they are never pushed from a workstation.
