"""Crucible: a deterministic supervisor for AI coding workers."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # Set at build time from the git tag (hatch-vcs), so /v1/health reports the
    # released version without a version constant living in the tree.
    __version__ = _package_version("crucible")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
