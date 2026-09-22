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

## C7g follow-up: Claude Code image pin and range agreement

The Claude Code image was rebuilt at 2.1.278. The downloaded linux-x64
binary reported `2.1.278 (Claude Code)` and had SHA-256
`5c4735937844e84f8a93306e841a5b0e12252909b07870f789b190468da147ab`.
The manifest now records the rebuilt tag and OCI digest.

Rootless build, 2026-09-21: `crucible-worker:claude_code-2.1.278-632c0cc67e82`,
OCI digest `sha256:a75f5d1d2ad68d6c772e7faa44b06854a8b470bf0fd74fffac98d2ceeddc5555`,
image ID `sha256:5dc2def90a1adfc0119cc86146d342cec9e47033ef4fffb35016cdd71c21bfd6`,
size `405960131` bytes.

`test_every_manifest_harness_version_is_supported_by_its_adapter` parses every
declared worker-image tag and checks its version against the matching adapter's
supported range. Before this correction, the Claude Code pin was 2.1.273 while
the adapter floor was 2.1.277, so no check tied the two declarations together.

The review round challenged whether a plausible manifest tag could name a binary
at another version. The unit test is intentionally limited to the declaration
seam, while the rootless build verification ran the resulting image's `claude
--version`; its label and executable both reported `2.1.278`. The checksum was
computed directly with `sha256sum` over the downloaded release binary before
the build, then the image build's `ADD --checksum` verified those same bytes.
