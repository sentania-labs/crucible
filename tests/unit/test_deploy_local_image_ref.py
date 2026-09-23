"""`make deploy-local` claims to honour a digest pin (README.md), but
`${DEPLOY_IMAGE##*:}` took everything after the *last* colon, which for
`repo:tag@sha256:...` is the digest's hex suffix, not the tag: the final `/v1/health`
version check would then compare against the wrong string and fail every digest-pinned
deployment (FDY-0089 review round).

`tools/deploy/deploy_local.sh` needs a real `crucible` service user, passwordless sudo
and a rootless Docker daemon to run past this point, none of which a unit test may
assume or touch. So the image-reference parsing block is extracted from the actual
script by the two literal strings that bound it and exercised on its own, with `die`
stubbed to exit 2 instead of aborting a real deployment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "deploy" / "deploy_local.sh"
START = '[ -n "$DEPLOY_IMAGE" ]'


def _parsing_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(START)
    tail = text[start:]
    first_esac = tail.index("esac") + len("esac")
    second_esac = tail.index("esac", first_esac) + len("esac")
    return tail[:second_esac]


def _deploy_version(image: str) -> tuple[int, str, str]:
    script = (
        "set -euo pipefail\n"
        'die() { echo "deploy-local: $*" >&2; exit 2; }\n'
        f'DEPLOY_IMAGE="{image}"\n'
        f"{_parsing_block()}\n"
        "printf '%s' \"$deploy_version\"\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def test_a_tag_only_reference_derives_its_tag() -> None:
    code, out, _ = _deploy_version("ghcr.io/sentania-labs/crucible:0.5.0")
    assert (code, out) == (0, "0.5.0")


def test_a_tag_and_digest_reference_derives_the_tag_not_the_digest() -> None:
    code, out, _ = _deploy_version("ghcr.io/sentania-labs/crucible:0.5.0@sha256:" + "a" * 64)
    assert (code, out) == (0, "0.5.0")


def test_a_digest_only_reference_has_no_tag_to_check_health_against() -> None:
    code, out, _ = _deploy_version("ghcr.io/sentania-labs/crucible@sha256:" + "a" * 64)
    assert (code, out) == (0, "")


def test_latest_is_refused_with_or_without_a_digest() -> None:
    for image in (
        "ghcr.io/sentania-labs/crucible:latest",
        "ghcr.io/sentania-labs/crucible:latest@sha256:" + "a" * 64,
    ):
        code, _, err = _deploy_version(image)
        assert code == 2
        assert "'latest' is not one" in err


def test_a_reference_with_neither_tag_nor_digest_is_refused() -> None:
    code, _, err = _deploy_version("ghcr.io/sentania-labs/crucible")
    assert code == 2
    assert "must carry an exact version tag or digest" in err


def test_the_image_reference_reaches_compose_and_env_unchanged() -> None:
    """The parsing block only derives local variables; DEPLOY_IMAGE itself, which
    becomes CRUCIBLE_DEPLOY_IMAGE in the Makefile, is never rewritten before it is
    written into compose.deploy.yaml and the deployment's .env."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "image: ${DEPLOY_IMAGE}" in text
    assert "CRUCIBLE_IMAGE=${DEPLOY_IMAGE}" in text
