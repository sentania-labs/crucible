"""The GitHub transport's rate-limit, pagination, and redirect rules (hades FDY-0139)."""

from __future__ import annotations

import json
from http.client import IncompleteRead
from typing import Any

import pytest

from crucible.adapters.github.transport import RestTransport
from crucible.domain.lifecycle import TaskState, is_allowed
from crucible.ports.github import GitHubError


class _Answer:
    """A canned response for one request, recording what was asked."""

    def __init__(
        self,
        seen: list[dict[str, Any]],
        status: int,
        body: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._seen = seen
        self._status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._headers = headers or {}
        self._read = False

    def request(self, method: str, url: str, body: Any = None, headers: Any = None) -> None:
        self._seen.append({"method": method, "url": url, "headers": dict(headers or {})})

    def getresponse(self) -> _Answer:
        return self

    @property
    def status(self) -> int:
        return self._status

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())

    def close(self) -> None:
        return


def _transport(answers: list[_Answer]) -> RestTransport:
    queue = list(answers)
    return RestTransport(
        "https://api.github.com", connection_factory=lambda host, port, timeout: queue.pop(0)
    )


def test_a_rate_limit_is_raised_at_once_with_the_wait_github_asked_for() -> None:
    seen: list[dict[str, Any]] = []
    transport = _transport(
        [
            _Answer(
                seen,
                403,
                {"message": "API rate limit exceeded"},
                {"x-ratelimit-remaining": "0", "retry-after": "42"},
            )
        ]
    )
    with pytest.raises(GitHubError) as caught:
        transport.get("/repos/o/r/pulls/1", bearer="t")
    assert caught.value.response_class == "rate_limited"
    assert caught.value.retry_after == 42.0
    # One call, no retry, no sleep: the caller defers to a later tick.
    assert len(seen) == 1
    assert transport.rate_limited == 1


def test_an_object_shaped_list_is_read_past_its_first_page() -> None:
    seen: list[dict[str, Any]] = []
    transport = _transport(
        [
            _Answer(
                seen,
                200,
                {"total_count": 3, "check_runs": [{"id": 1}, {"id": 2}]},
                {"link": '<https://api.github.com/x?page=2>; rel="next"'},
            ),
            _Answer(seen, 200, {"total_count": 3, "check_runs": [{"id": 3}]}),
        ]
    )
    rows = transport.paginate("/repos/o/r/commits/abc/check-runs", bearer="t", key="check_runs")
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert "page=2" in seen[1]["url"]


def test_a_log_download_sends_no_token_and_keeps_the_tail() -> None:
    seen: list[dict[str, Any]] = []
    body = b"early lines\n" * 100 + b"the failure\n"
    transport = RestTransport(
        "http://127.0.0.1:9",
        connection_factory=lambda host, port, timeout: _Answer(seen, 200, body),
    )
    tail = transport.download("http://127.0.0.1:9/_signed/job/1?sig=x", limit_bytes=24)
    assert tail == body[-24:]
    assert "Authorization" not in seen[0]["headers"]


class _Dropped(_Answer):
    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(b"partial")


def test_a_log_download_cut_off_partway_is_a_github_error_not_a_crash() -> None:
    """IncompleteRead is not an OSError; it must not escape into the supervisor's tick."""
    seen: list[dict[str, Any]] = []
    transport = RestTransport(
        "http://127.0.0.1:9",
        connection_factory=lambda host, port, timeout: _Dropped(seen, 200, b""),
    )
    with pytest.raises(GitHubError) as caught:
        transport.download("http://127.0.0.1:9/_signed/job/1", limit_bytes=10)
    assert caught.value.response_class == "transport"


def test_a_log_redirect_to_plain_http_elsewhere_is_refused() -> None:
    transport = RestTransport("https://api.github.com")
    with pytest.raises(GitHubError):
        transport.download("http://logs.example.invalid/job/1", limit_bytes=10)


@pytest.mark.parametrize(
    "state",
    [
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.CI_CERTIFICATION_FAILED,
        TaskState.HEAD_DIVERGED,
        TaskState.READY_FOR_MERGE,
    ],
)
def test_an_observed_merge_is_allowed_from_every_delivery_state(state: TaskState) -> None:
    assert is_allowed("task", state, TaskState.MERGED)
    assert is_allowed("task", state, TaskState.REJECTED)
