# Kubernetes provider defects (FDY-0121)

Seven findings from the adversarial reviews of PR 52 and PR 53, fixed together on the
operator's go of 2026-09-25 ("Go on everything", 12:49 PM). What each fix does is in
spec 26 and spec 12; this note records the decisions behind them.

## Decisions

- **Sync-back is not gated on the exit code (56).** Spec 12 said "on clean exit" while
  spec 26, both providers and the C8b decision sync a valid, newer auth file whenever
  the attempt reaches collection. The spec was amended rather than the code gated: a
  harness that refreshed before the task failed has rotated its refresh token, so
  keeping the old one would lock every later worker out, and a successful exit says
  nothing about whether a token file is genuine. The shape and issued-at checks are
  the guard, on every path.
- **The PID limit is the operator-declared path (60).** The Pod API has no per-pod PID
  field: a container's `resources` and pod-level `resources` both refuse `pids`, which
  the kind tier proves against a real API server. The operator accepted the
  operator-declared path on 2026-09-23 on condition that it is documented; it is now
  in spec 26 (pod shape and checklist item 4) and in `docs/deployment.md`.
- **`broad_egress` and `resolve_ttl_seconds` are restart-bound settings (61).** They sit
  with the other Kubernetes deployment settings: the settings file, the environment,
  the base ConfigMap, and the admin UI's settings page. They are deliberately not
  runtime-editable through `kubernetes.egress`: `broad_egress` lets a worker reach
  GitHub, which spec 26 forbids, and a change of that weight belongs to a deployment
  change with a restart, not to one form submission.
- **Log reads are bounded, and a crowded second is skipped with a notice (63).**
  4 MiB a poll, raised to 64 MiB only for a second that holds more than one read, and
  past that the rest of the second is replaced by a `[crucible] log lines skipped`
  line. `sinceTime` is one-second granular, so no request can resume inside such a
  second; the alternative, an unbounded read, is what the issue was about. The resume
  position after a skip names no line (`RESUME_AT_BOUNDARY` in `logstream`), so the
  first line of the next second is kept.
- **Limits are read back from the live Pod (66, 76).** Adoption already took the grace
  period from the Job template (C8b, #53); it now takes every limit from the live Pod
  when there is one, `observe` does the same for a Pod it launched, `terminate` reads
  the grace off the Pod even for a handle it never saw, and the launch evidence says
  whether its limits came from the Pod or the policy.
- **Readiness gates `prepare` (59).** The image is resolved before the gate, because the
  canary runs the image an attempt resolved when no `probe_image` is configured.

## Left as they were

- The worker's log tail at collection and the login Job's log still read the whole
  Pod log, once per attempt and per login poll respectively. Both are small in
  practice; neither is an observation poll.
