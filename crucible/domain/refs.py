"""What a git ref may be called (05).

Every ref a contract names is interpolated into a command line sooner or later: the
preparer clones and checks out with it, the collector ranges over it, the publisher
will push it. Shell quoting is what keeps a ref from executing, and this is what keeps
it from being an option or a path that means something else. Both, not either.
"""

from __future__ import annotations

import re

# A ref is letters, digits, and the four punctuation marks git itself uses in branch
# names. No spaces, no shell metacharacters, no backslashes, nothing from another
# encoding: a ref outside this set is refused rather than escaped and hoped for.
REF_CHARACTERS = re.compile(r"\A[A-Za-z0-9._/-]+\Z")
MAX_REF_LENGTH = 255


class InvalidRefError(ValueError):
    """A ref a contract named cannot be used safely."""


def ref_problem(value: str) -> str | None:
    """The reason this ref is not usable, or None. Pure; no git, no filesystem."""
    if not value:
        return "must not be empty"
    if len(value) > MAX_REF_LENGTH:
        return f"must be at most {MAX_REF_LENGTH} characters"
    if not REF_CHARACTERS.match(value):
        return "may only contain letters, digits, and the characters . _ / -"
    if value.startswith("-"):
        # `git checkout -B -x` reads the ref as an option.
        return "must not begin with '-', which git reads as an option"
    if value.startswith("/") or value.endswith("/") or "//" in value:
        return "must not begin or end with '/' or contain an empty path component"
    if ".." in value or value.endswith(".lock") or "@{" in value:
        return "must not contain '..' or '@{' or end with '.lock' (git refuses them)"
    for component in value.split("/"):
        if component in (".", ".."):
            return "must not contain a '.' or '..' path component"
        if component.startswith("."):
            return "must not contain a path component beginning with '.'"
    return None


def check_ref(value: str, *, field: str = "ref") -> str:
    """Return the ref, or raise. Used where a refusal is the only safe answer."""
    problem = ref_problem(value)
    if problem is not None:
        raise InvalidRefError(f"{field} {value!r} {problem}")
    return value
