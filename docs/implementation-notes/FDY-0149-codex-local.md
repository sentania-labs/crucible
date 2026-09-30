# FDY-0149: local Codex, issue #249

The operator requested Codex alongside Hermes on the Local gateway page, with Codex
preferred for trivial and standard work. The live trial on 2026-09-29, recorded in
https://github.com/sentania-labs/hades/issues/249, used the same tasks, prompts and
qwen3.6-35B engine under equalized conditions: full-auto file and shell tools, an
80-turn cap where supported, and a 45-minute wall clock. Codex passed and committed
3/3; Hermes passed 3/3 and committed 2/3. On Qwen3-Coder-Next Codex passed 2/3 and
Hermes 1/4. One Codex loss was a client hang after a normal response; Hermes kept
working on that engine, supporting its role as fallback.

The trial established the Responses API requirement (chat wire support was removed
in Codex 0.155), a writable CODEX_HOME outside /tmp containing the provider config,
no --ignore-user-config, closed stdin, the sandbox bypass inside an isolated Pod,
and exact gateway model aliases. This implementation preserves the finite stdin
pipe already used by both providers and tests it by reading to EOF. It writes the
local config before process launch and selects the existing read-only gateway key,
never the subscription credential. No deployment or image changes are required.

The existing context-length control is shared. Its zero discovery setting cannot
invoke Hermes discovery in Codex, so Codex uses the documented default 131072;
operators can set the exact positive window on the same page. Hermes turn limits
are not presented as a Codex turn cap.
