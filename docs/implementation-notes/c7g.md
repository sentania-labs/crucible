# C7g: one identity shim

Crucible now writes only an untracked `AGENTS.md` shim for Claude Code,
Codex, and AGY. Claude Code skips that shim when the checkout already has a
project `CLAUDE.md`, because that file wins under the default
`instructionFiles` setting. The no-injected-files gate prohibits both
`AGENTS.md` and `CLAUDE.md`, so a worker cannot make either instruction file
part of its branch.

Claude Code support starts at 2.1.277. The primary source is the Claude Code
changelog: https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md,
section 2.1.277, which says that release added `AGENTS.md` support. The
pinned 2.1.273 worker image is therefore refused until it is rebuilt by the
image owner. This change does not rebuild or re-pin images.

The old two-name exclude list is reduced to `/AGENTS.md`. The gate still
prohibits `CLAUDE.md` so a worker that writes one by hand fails collection.
