# Spike results (Phase C0)

One file per spike from `docs/spec/21-spikes.md`, each with the commands
run, output excerpts, and a decision: proceed, adjust design, or escalate.
Nothing here is product code. A spike that fails changes the specification
before C1 starts.

| ID | Question | Result |
|---|---|---|
| S1b | Do dedicated Crucible credential sessions leave the operator's daily sessions valid? | pass on exercised steps; Claude Code verified; Codex and AGY pending their Crucible-side refresh ([S1b.md](S1b.md)) |
| S1 | Does each harness's subscription auth work from a mounted config directory inside a non-root container? | adjust design: all three complete a prompt as uid 1000 from a mounted copy; Claude Code `rw-narrow` shape confirmed; Codex and AGY refreshed their own tokens on the first run, which stopped their authenticated spikes and is escalated ([S1.md](S1.md)) |
| S2 | Can Codex's own sandbox run inside the container? | proceed, not enabled: no bubblewrap in the image or binary, and user namespaces are unavailable under the hardened shape on this host ([S2.md](S2.md)) |
| S3 | Does AGY operate headless with `--add-dir`, and what is its argv ceiling? | proceed, partial: headless flags and stream-json confirmed; the ceiling is the Linux 128 KiB per-argument limit; the 100 KB bundle run is blocked on the S1 finding ([S3.md](S3.md)) |
| S4 | With harness sandboxes disabled, does the container hardening hold? | proceed, partial: every probe failed on the rootful daemon, but the credential, database and proxy checks ran against an empty environment; complete on rootless in C3 with the real control plane and credentials present ([S4.md](S4.md)) |
| S5 | Is exit-code and report-file detection reliable? | proceed, with `--init` added to the worker launch so SIGTERM reaches a harness running as PID 1 ([S5.md](S5.md)) |
| S6 | Which egress endpoints does each harness need? | adjust design: all three allowlists provisional (Claude Code discovered live but never run behind the filter alone); complete with an authenticated run through the actual filter in C3; Codex gains `--disable plugins` ([S6.md](S6.md)) |
| S7 | Can harness versions be pinned and updated predictably in images? | proceed, partial: pinned and reproducible at one version each; the second-version update is not exercised until a new pin lands ([S7.md](S7.md)) |
| S8 | Does a running worker survive a supervisor restart? | proceed, partial: survives restart of a stand-in supervisor container with logs resumable by offset; Crucible itself and `docker compose restart` repeat in C3 ([S8.md](S8.md)) |
| S11 | Can each harness be prevented from self-updating, and does it report its version reliably? | proceed for Claude Code (confirmed in an authenticated run); partial for Codex and AGY (unauthenticated runs only); versions match labels ([S11.md](S11.md)) |
| S10 | Can Crucible mint a repository-scoped installation token and push a branch and an annotated tag from a publisher container without the token leaving tmpfs? | proceed: branch, annotated tag, and PR delivered through the App token; token absent from every checked location, with positive controls proving the scanner detects it in a buffer and across a streaming read boundary; hand-over is stdin, not `docker cp`; Contents write alone pushes tags ([S10.md](S10.md)) |
| S12 | What does the external reviewer emit on a PR, under which login, and how is it triggered? | adjust design: with the repository set to review all PRs, an App-authored PR was reviewed automatically in 101 s under `chatgpt-codex-connector[bot]`; that clean review was only an `eyes` then `+1` reaction on the PR, which the App's grant cannot read (403), so Issues read is required and the provider setting, invisible to GitHub, is a repository-onboarding prerequisite ([S12.md](S12.md)) |
| S15 | Does `quota-axi` supply a usable free-capacity reading for all three providers, in the `crucible` container, from the dedicated credentials, against read-only mounts? | adjust design, proceed with conditions: the first real `exhausted_now` was captured (Codex, weekly window, 2026-09-18 23:47 CDT, host and container agreeing), Codex reads correctly in the service container from a read-only mount opening only `auth.json` and writing nothing, AGY cannot be read in the shipped image at all because it needs the `agy` CLI, and on a writable mount the AGY read rotated the dedicated credential and triggered the CLI's self-updater unless `--no-credential-refresh` is passed; Claude is still unreadable and is blocked on an operator oauth login; the dedicated Codex credential shares the operator's account-level weekly window ([S15.md](S15.md), ADR 0014 amendment proposed) |

Worker base images used by the spikes are built from `images/` with
`images/build.sh`; they are never pushed from a workstation.
