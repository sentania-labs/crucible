"""AGY adapter capabilities: endpoints and login_endpoints (FDY-0159).

Every secret-shaped value here is built at run time. Nothing in this repository is a
checked-in token, key, or signature.
"""

from __future__ import annotations

from crucible.adapters.harness.agy import AgyAdapter

_PROFILE_PICTURE_HOST = "lh3.googleusercontent.com"


def test_agy_login_egress_includes_the_profile_picture_host() -> None:
    """The profile picture host must be in both endpoints and login_endpoints.

    The worker's egress allowlist covers `endpoints`; the login Job's egress is built
    from `login_endpoints` only (crucible#58). After sign-in AGY's eligibility check
    fetches the account's profile picture from lh3.googleusercontent.com before any
    turn, so the login Job needs that host in its allowlist too (hades #241).
    """
    cap = AgyAdapter().capabilities()
    assert _PROFILE_PICTURE_HOST in cap.endpoints, (
        f"{_PROFILE_PICTURE_HOST} must be in endpoints (worker egress)"
    )
    assert _PROFILE_PICTURE_HOST in cap.login_endpoints, (
        f"{_PROFILE_PICTURE_HOST} must be in login_endpoints (login Job egress; hades #241)"
    )
