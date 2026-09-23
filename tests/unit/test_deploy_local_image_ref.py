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


def test_a_registry_port_is_not_mistaken_for_a_tag() -> None:
    """localhost:5000/crucible@sha256:... has a colon that belongs to the registry's
    port, not a tag; only the last path segment may carry a tag (review round)."""
    code, out, _ = _deploy_version("localhost:5000/crucible@sha256:" + "a" * 64)
    assert (code, out) == (0, "")


def test_a_registry_port_with_no_tag_or_digest_is_still_refused() -> None:
    code, _, err = _deploy_version("localhost:5000/crucible")
    assert code == 2
    assert "must carry an exact version tag or digest" in err


def test_a_registry_port_with_a_tag_reads_the_tag_not_the_port() -> None:
    code, out, _ = _deploy_version("localhost:5000/crucible:0.5.0")
    assert (code, out) == (0, "0.5.0")


LABEL_START = 'if [ -z "$deploy_version" ]; then'


def _label_fallback_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(LABEL_START)
    tail = text[start:]
    end = tail.index("\nfi\n") + len("\nfi")
    return tail[: end + 1]


def _fallback_deploy_version(
    label_output: str, *, deploy_version: str = ""
) -> tuple[int, str, str]:
    """The digest-only path (review round): with no tag, /v1/health is checked against
    the org.opencontainers.image.version label read back from the pulled image. `docker`
    is stubbed so this exercises the real script text without touching a real daemon."""
    script = (
        "set -euo pipefail\n"
        'die() { echo "deploy-local: $*" >&2; exit 2; }\n'
        'as_service_user() { "$@"; }\n'
        "docker() { printf '%s' \"$LABEL_OUTPUT\"; }\n"
        'DEPLOY_IMAGE="ghcr.io/sentania-labs/crucible@sha256:aaaa"\n'
        f'deploy_version="{deploy_version}"\n'
        f"{_label_fallback_block()}\n"
        "printf '%s' \"$deploy_version\"\n"
    )
    env = {"LABEL_OUTPUT": label_output}
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
    )
    return result.returncode, result.stdout, result.stderr


def test_a_digest_only_reference_takes_its_version_from_the_pulled_image_label() -> None:
    code, out, _ = _fallback_deploy_version("0.5.0")
    assert (code, out) == (0, "0.5.0")


def test_a_missing_version_label_is_refused_rather_than_silently_skipped() -> None:
    for label_output in ("", "<no value>"):
        code, _, err = _fallback_deploy_version(label_output)
        assert code == 2
        assert "carries no org.opencontainers.image.version label" in err


def test_a_reference_with_a_tag_never_consults_the_image_label() -> None:
    code, out, _ = _fallback_deploy_version("should-not-be-used", deploy_version="0.5.0")
    assert (code, out) == (0, "0.5.0")


def test_the_image_reference_reaches_compose_and_env_unchanged() -> None:
    """The parsing block only derives local variables; DEPLOY_IMAGE itself, which
    becomes CRUCIBLE_DEPLOY_IMAGE in the Makefile, is never rewritten before it is
    written into compose.deploy.yaml and the deployment's .env."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "image: ${DEPLOY_IMAGE}" in text
    assert "CRUCIBLE_IMAGE=${DEPLOY_IMAGE}" in text
