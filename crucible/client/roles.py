"""Which role the token in use holds, learned from the API and never assumed.

A command whose own route admits one role class has proven it: an admin route answered,
so the token is an admin's. A read admits every role, so the client asks the API with two
read-only probes before it offers any `next` action: `GET /v1/admin/audit?limit=1` (the
admin guard) and `GET /v1/capabilities` (the orchestrator guard). Neither records
anything; a 403 from either is the guard's answer, not a failure. Anything else (the API
down, the admin surface not configured) leaves the role unknown, and an unknown role
offers no actions.
"""

from __future__ import annotations

from crucible.client.envelope import ClientError
from crucible.client.http import ORCHESTRATOR_TIMEOUT_SECONDS, Api
from crucible.client.next import ADMIN, OBSERVER, PROBED_ORCHESTRATOR


def _passes(api: Api, path: str) -> bool | None:
    try:
        api.call("GET", path, timeout=ORCHESTRATOR_TIMEOUT_SECONDS)
    except ClientError as exc:
        if exc.status == 403:
            return False
        return None
    return True


def probe_role(api: Api) -> tuple[str | None, str | None]:
    """(role, why unknown). `orchestrator` stands for orchestrator or operator."""
    admin = _passes(api, "/v1/admin/audit?limit=1")
    if admin is True:
        return ADMIN, None
    if admin is None:
        return None, "the admin probe (GET /v1/admin/audit) did not answer 200 or 403"
    orchestrator = _passes(api, "/v1/capabilities")
    if orchestrator is True:
        return PROBED_ORCHESTRATOR, None
    if orchestrator is False:
        return OBSERVER, None
    return None, "the orchestrator probe (GET /v1/capabilities) did not answer 200 or 403"
