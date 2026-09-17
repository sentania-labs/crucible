"""Workspace layout for the Docker provider (08).

Crucible's own image carries no git, and 08 already says the collector, not the
Crucible process, is what reads a worker's tree. C3 applies the same rule to
preparation: every git command runs in a throwaway container from the worker image
(`scripts.preparer_script`), and what Crucible writes here is plain files it owns.

What the worker ends up with is a checkout whose origin resolves nowhere, an author
identity from policy, no credential helper, the shims listed in `.git/info/exclude`,
a read-only identity bundle, and an empty report directory.
"""

from __future__ import annotations

# The origin URL a worker sees. It resolves nowhere, so a push cannot even start (S4).
ORIGIN_PLACEHOLDER = "crucible-no-remote://this-checkout-cannot-push"
SHIM_NAMES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
EXCLUDE_ENTRIES: tuple[str, ...] = (
    "# Written by Crucible at prepare; these are shims, not work (06, 11).",
    "/CLAUDE.md",
    "/AGENTS.md",
    "/.crucible/",
)


class WorkspaceError(Exception):
    """Preparation failed. The attempt is an `environment` failure (16)."""
