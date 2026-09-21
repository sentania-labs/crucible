# C6d: Hermes on the DGX Spark local pool

## Delivered behavior

- Hermes 0.19.0 runs as UID 1000 in a reproducible, read-only worker image. The
  declared image pin and OCI digest live in `images/manifest.env`.
- Local endpoint URL data travels from the routing policy through `LaunchSpec` and
  `LaunchContext`. Subscription routes reject a URL, local launches require one, and
  Hermes accepts only `gpt-oss:120b`.
- Hermes receives no credential mount. Its literal placeholder API key is not a
  credential and only satisfies the OpenAI client library's required field.
- The generated Squid policy permits the configured Spark destination and port only.
  The local port is added to `Safe_ports`, never `SSL_ports`.
- Hermes usage JSON is mandatory run evidence. Tokens come from that file when the
  provider reports them. Duration and tool-call count are enriched from the per-attempt
  SQLite state before the worker exits. The home directory is tmpfs and is not retained.
- Routing version 4 carries the disabled local entry and its reason. A deployment with
  `CRUCIBLE_SPARK_ENDPOINT_URL` also receives version 5, enabled only after the live
  single-task and four-slot checks passed.

## Local evidence

At 8:16:39 PM CDT on 2026-09-20, Hermes used the real proxy and `gpt-oss:120b` to
complete one task in 77.9 seconds. It exited 0, passed all pre-PR gates, reached
`ready_for_merge`, and reported 4,746 input tokens, 2,765 output tokens, 72,974 ms of
harness duration, and 5 tool calls. The temporary pull request and branch were cleaned
up by the tier. Its required-check run is
https://github.com/sentania-labs/crucible-spike-target/actions/runs/35550459158.

The live pool test then observed four Hermes workers in `running` while a fifth task in
`spark-local` remained `scheduled`. The fifth task recorded one durable
`harness_launch_deferred` event naming the four-of-four pool limit.

## Review

The required adversarial review and any non-blocking findings are recorded here before
the pull request opens.
