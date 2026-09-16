# 22. Unresolved questions for the operator

Decisions Crucible cannot make. Each has a recommendation; the specification
proceeds on the recommendation unless told otherwise.

1. **Worker image registry.** Publish harness base images to
   `ghcr.io/sentania-labs/` from Crucible's CI, or build locally only in
   v0.x? Recommendation: build locally in C3, publish from C4 when the
   release workflow exists, because a public image with a pinned harness
   version is useful to others and costs nothing on a public repo.
2. **Repository credential type.** A fine-grained personal access token per
   target repository, or a GitHub App installation? Recommendation: App,
   because installation tokens are short-lived and scoped by repository,
   which is the only branch-safety mechanism Crucible has beyond the gate.
   Needs the operator to create the App.
3. **Review artifact source.** The non-author review before a PR is a
   Foundry act; Crucible only checks that a review artifact exists for the
   SHA. Should Crucible run the reviewer as a second worker attempt with a
   `reviewer` role (a Crucible feature) or should Foundry run it through its
   own harness and upload the artifact? Recommendation: Crucible runs it as
   a role, from C4, because it keeps evidence in one place and the reviewer
   then also runs isolated. Until then Foundry uploads.
4. **External review bot.** The `external_review_round` gate assumes a
   reviewer bot on the target repository. Which bot and how is "reviewed,
   clean" signaled (reaction, review, comment)? Recommendation: configurable
   per policy with reaction and review both accepted; default to `pending`
   tolerance so it never blocks acceptance.
5. **Timezone for rendering.** Stored UTC with offset; rendered in one
   configured zone. Recommendation: `America/Chicago` in the operator's
   private configuration, not in the repository default (which stays UTC).
6. **Concurrency default.** 3 per provider, 1 per harness until S1 resolves.
   Confirm or set.
7. **Retention defaults.** 90 days for transcripts and logs, 180 for the
   bootstrap archive, indefinite for everything else. Confirm.
8. **Host-process provider.** Include it in v0.x code at all, or only in
   the design? Recommendation: design only until a harness proves it cannot
   run in a container; S1 to S3 answer that.
9. **Crucible's own delivery.** Crucible is consumed by Foundry, so it is a
   branch-and-PR repository with tagged releases per the delivery pipeline.
   Its CI needs a Docker daemon, so it runs GitHub-hosted. Confirm.
11. **Docker socket blast radius.** In the default local mode a Crucible
    compromise is root-equivalent on the host (13, ADR 0004); the proxy
    reduces surface but cannot validate request bodies. Accept that for a
    single-operator workstation, or run a rootless Docker daemon dedicated
    to Crucible so an escape yields an unprivileged user? Recommendation:
    accept for C1 to C3 with the risk recorded, and make rootless Docker a
    C3 acceptance item if the workstation supports it (S9, added to 21).
12. **Sandcastle code reuse.** None proposed. If a specific helper (the
    Docker volume label formatting, the idle-timeout state machine) proves
    worth porting, it gets an ADR with attribution. Confirm no default reuse.
