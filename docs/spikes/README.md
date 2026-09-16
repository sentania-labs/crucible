# Spike results (Phase C0)

One file per spike from `docs/spec/21-spikes.md`, each with the commands
run, output excerpts, and a decision: proceed, adjust design, or escalate.
Nothing here is product code. A spike that fails changes the specification
before C1 starts.

| ID | Question | Result |
|---|---|---|
| S7 | Can harness versions be pinned and updated predictably in images? | proceed, partial: pinned and reproducible at one version each; the second-version update is not exercised until a new pin lands ([S7.md](S7.md)) |
| S8 | Does a running worker survive a supervisor restart? | proceed, partial: survives restart of a stand-in supervisor container with logs resumable by offset; Crucible itself and `docker compose restart` repeat in C3 ([S8.md](S8.md)) |
| S10 | Can Crucible mint a repository-scoped installation token and push a branch and an annotated tag from a publisher container without the token leaving tmpfs? | proceed: branch, annotated tag, and PR delivered through the App token; token absent from every checked location, with a positive control proving the scanner detects it; hand-over is stdin, not `docker cp`; Contents write alone pushes tags ([S10.md](S10.md)) |
| S12 | What does the external reviewer emit on a PR, under which login, and how is it triggered? | adjust design: login `chatgpt-codex-connector[bot]` and signal shapes confirmed from the live record; no review on a PR opened by the App in 20 min and `@codex review` from the App is refused, so the trigger needs a connected human; PR-level reactions need Issues read ([S12.md](S12.md)) |

Worker base images used by the spikes are built from `images/` with
`images/build.sh`; they are never pushed from a workstation.
