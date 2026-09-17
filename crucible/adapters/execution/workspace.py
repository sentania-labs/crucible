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

from pathlib import Path

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


def write_shims(repo: Path, identity_mount: str) -> list[str]:
    """Write the harness shims the checkout lacks and exclude them from the diff (06)."""
    written: list[str] = []
    for name in SHIM_NAMES:
        target = repo / name
        if target.exists():
            continue
        target.write_text(
            f"Read {identity_mount}/IDENTITY.md first; it is the task contract for this run.\n",
            encoding="utf-8",
        )
        written.append(name)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [line for line in EXCLUDE_ENTRIES if line not in existing]
    if missing:
        separator = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + separator + "\n".join(missing) + "\n", encoding="utf-8")
    return written
