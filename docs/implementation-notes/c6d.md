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
- Routing version 4 carries the disabled local entry and its reason. Routing version 5
  is added only after the live single-task and four-way checks pass.

## Local evidence

At 9:30:38 PM CDT on 2026-09-20, Hermes used the real proxy and `gpt-oss:120b` to
complete task `01M30WT5QZDCFFADQ80Z0WARQT`, attempt
`01M30WT5SY2W5DM8N2AGPJN9GS`, in 147.8 seconds. It exited 0, passed every pre-PR
gate, reached `ready_for_merge`, appeared in routing history, and recorded
AttemptMetrics with 6,127 input tokens, 5,305 output tokens, 141,133 ms of harness
duration, and 16 tool calls. The tier closed temporary pull request 103 and removed
its branch.

At 9:41:05 PM CDT on 2026-09-20, the live pool test observed four Hermes workers in
`running` while a fifth task and attempt `01M30XDAE3827YZWPNV15HY9E8` remained
`scheduled`. The fifth recorded a durable `harness_launch_deferred` event naming the
four-of-four pool limit. The four isolated attempts then exited 0, parsed their reports,
and passed every pre-PR gate:

- `01M30XDADEVW2XTDHB61QHZR4D`: 338.1 seconds, 7,828 input tokens, 6,625 output
  tokens, 15 tool calls.
- `01M30XDADMAAZNXJCF67ZPAB8K`: 271.5 seconds, 3,451 input tokens, 3,515 output
  tokens, 9 tool calls.
- `01M30XDADSTFAZ125J0Z45PR08`: 298.9 seconds, 6,310 input tokens, 5,157 output
  tokens, 6 tool calls.
- `01M30XDADY3NTZ8THC9073M5MJ`: 216.0 seconds, 5,087 input tokens, 3,408 output
  tokens, 9 tool calls.

## Review

The single required non-author adversarial review found four blockers. The live pool
test had only observed admission, the single live test did not require routing-history
and metrics evidence, Squid accepted a configured URL without consulting enabled
routing entries, and Hermes unit coverage did not assert the complete launch and
classification contract. The branch now completes all four tasks before enabling,
requires both evidence records, filters Squid inputs to enabled local policy entries,
and covers the exact argv, environment, credential absence, and classification order.

The reviewer also noted stale specs and two transcript writers. The named spec sections
now describe C6d, `provider_error`, version 4 and 5, the pool cap, and the local proxy
rules. Hermes now inherits stdout so Crucible's launch wrapper is the sole transcript
writer. No review finding remains open.
