"""The manifests-are-examples callout is repeated everywhere a deployer looks first.

A grep-style check over the files the operator named: `docs/deployment.md`,
`README.md`, the Argo Application template and the compose defaults. Losing the
callout from any one of them is a silent regression a reader would not otherwise
catch, so its exact words are asserted here rather than left to review alone.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CALLOUT_FRAGMENTS = (
    "examples that track `latest`",
    "the deployer's own repository and pins the exact tag and digest there",
)

FILES_WITH_CALLOUT = (
    "docs/deployment.md",
    "README.md",
    "deploy/kubernetes/argocd/application.yaml",
    "compose.yaml",
    ".env.example",
)


def test_the_callout_sentence_is_in_every_deployer_facing_file() -> None:
    for relative in FILES_WITH_CALLOUT:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for fragment in CALLOUT_FRAGMENTS:
            assert fragment in text, f"{relative} is missing {fragment!r}"
