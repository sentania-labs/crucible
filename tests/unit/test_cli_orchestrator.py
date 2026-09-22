"""The orchestrator verbs of `crucible`, carried from Foundry's `foundry-crucible`: the
same requests, now answered with the envelope (docs/client.md). Every request goes to a
local fake; no test reaches a real Crucible."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from crucible.cli.main import run
from tests.fake_crucible import TOKEN, FakeCrucible, fake

__all__ = ["fake"]


@pytest.fixture(autouse=True)
def _orchestrator(fake: FakeCrucible) -> None:
    fake.role("orchestrator")


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1, captured.out
    document = json.loads(lines[0])
    assert isinstance(document, dict)
    return document


def _document(tmp_path: Path, name: str = "input.json") -> Path:
    path = tmp_path / name
    path.write_text('{"schema_version":"1.0","value":"input"}\n', encoding="utf-8")
    return path


REQUEST_SHAPES = [
    (["tasks"], "GET", "/v1/tasks", None),
    (["tasks", "--state", "running"], "GET", "/v1/tasks?state=running", None),
    (["task", "T1"], "GET", "/v1/tasks/T1", None),
    (["wakes"], "GET", "/v1/wakes", None),
    (
        ["wakes", "ack", "W1", "--reason", "handled"],
        "POST",
        "/v1/wakes/W1/ack",
        {"note": "handled"},
    ),
    (
        ["start", "T1", "--policy-version", "2", "--model", "model-a", "--reason", "go"],
        "POST",
        "/v1/tasks/T1/start",
        {"model": "model-a", "policy_version": 2},
    ),
    (
        ["accept", "T1", "--verdict", "accepted", "--head-sha", "abc", "--reason", "ok"],
        "POST",
        "/v1/tasks/T1/accept",
        {"verdict": "accepted", "reasoning": "ok", "head_sha": "abc"},
    ),
    (
        ["ci-decision", "T1", "--cause", "code", "--action", "correct", "--reason", "fix"],
        "POST",
        "/v1/tasks/T1/ci-decision",
        {"cause": "code", "action": "correct", "reasoning": "fix"},
    ),
    (
        ["head-decision", "T1", "--action", "recollect", "--reason", "inspect"],
        "POST",
        "/v1/tasks/T1/head-decision",
        {"action": "recollect", "reasoning": "inspect"},
    ),
    (
        [
            "decisions",
            "T1",
            "--kind",
            "scope",
            "--verbatim",
            "Proceed",
            "--resolves",
            "E1",
            "--reschedule",
            "--reason",
            "operator decided",
        ],
        "POST",
        "/v1/tasks/T1/decisions",
        {"kind": "scope", "verbatim": "Proceed", "resolves": "E1", "reschedule": True},
    ),
    (
        [
            "cancel",
            "T1",
            "--verbatim",
            "Cancel it",
            "--decided-by",
            "operator",
            "--reason",
            "obsolete",
        ],
        "POST",
        "/v1/tasks/T1/cancel",
        {"reason": "obsolete", "verbatim": "Cancel it", "decided_by": "operator"},
    ),
    (["close", "T1", "--reason", "complete"], "POST", "/v1/tasks/T1/close", {"note": "complete"}),
    (
        ["republish", "T1", "--reason", "retry fixed provider"],
        "POST",
        "/v1/tasks/T1/republish",
        {"reason": "retry fixed provider"},
    ),
    (["health"], "GET", "/v1/health", None),
]


@pytest.mark.parametrize(("argv", "method", "path", "body"), REQUEST_SHAPES)
def test_command_request_shapes(
    fake: FakeCrucible,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    method: str,
    path: str,
    body: Any,
) -> None:
    if argv[0] in {"tasks", "wakes"} and "ack" not in argv:
        fake.response = {"schema_version": "1.0", "items": [], "next_cursor": None}
    assert run(argv) == 0
    assert fake.calls()[0] == (method, path, body)
    request = next(r for r in fake.requests if r["path"] == path)
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    if "--reason" in argv:
        assert request["headers"]["X-Foundry-Reason"] == argv[argv.index("--reason") + 1]
    assert _envelope(capsys)["ok"] is True


@pytest.mark.parametrize("command", ["submit", "review", "dispositions", "corrections"])
def test_file_command_request_shapes(
    fake: FakeCrucible, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    argv = [command]
    expected_path = "/v1/tasks"
    if command != "submit":
        argv.append("T1")
        expected_path = f"/v1/tasks/T1/{command}"
    argv.extend([str(_document(tmp_path)), "--reason", "needed"])
    assert run(argv) == 0
    request = fake.requests[0]
    assert (request["method"], request["path"]) == ("POST", expected_path)
    assert request["body"] == {"schema_version": "1.0", "value": "input"}
    assert request["headers"]["X-Foundry-Reason"] == "needed"
    assert _envelope(capsys)["kind"] == "task"


MUTATIONS = [
    ["wakes", "ack", "W1"],
    ["submit", "missing.json"],
    ["start", "T1", "--policy-version", "2"],
    ["accept", "T1", "--verdict", "accepted"],
    ["review", "T1", "missing.json"],
    ["dispositions", "T1", "missing.json"],
    ["corrections", "T1", "missing.json"],
    ["ci-decision", "T1", "--cause", "code", "--action", "correct"],
    ["head-decision", "T1", "--action", "reject"],
    ["decisions", "T1", "--kind", "scope", "--verbatim", "yes", "--resolves", "E1"],
    ["cancel", "T1", "--verbatim", "cancel"],
    ["close", "T1"],
    ["republish", "T1"],
]


@pytest.mark.parametrize("argv", MUTATIONS)
def test_every_mutation_refuses_without_reason(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert run(argv) == 2
    assert not fake.requests
    document = _envelope(capsys)
    assert document["ok"] is False and document["error"]["code"] == "usage"
    assert "--reason" in document["error"]["message"]


def test_task_related_flags_reach_all_records(fake: FakeCrucible) -> None:
    fake.responses = [
        {
            "schema_version": "1.0",
            "id": "T1",
            "state": "running",
            "latest_attempt": {"id": "A1"},
            "executions": [{"attempts": [{"id": "A1"}]}],
        },
        {"schema_version": "1.0", "items": [], "next_cursor": None},
        {"schema_version": "1.0", "pull_request": None},
        {"schema_version": "1.0", "id": "A1"},
        {"schema_version": "1.0", "items": [], "next_cursor": None},
        {"schema_version": "1.0", "attempt_id": "A1", "document": {}},
        {"schema_version": "1.0", "items": [], "next_cursor": None},
    ]
    argv = ["task", "T1", "--events", "--pull-request", "--attempts", "--gates", "--report"]
    assert run([*argv, "--evidence"]) == 0
    assert [(method, path) for method, path, _ in fake.calls()] == [
        ("GET", "/v1/tasks/T1"),
        ("GET", "/v1/tasks/T1/events"),
        ("GET", "/v1/tasks/T1/pull-request"),
        ("GET", "/v1/attempts/A1"),
        ("GET", "/v1/attempts/A1/gates"),
        ("GET", "/v1/attempts/A1/report"),
        ("GET", "/v1/attempts/A1/evidence"),
    ]


@pytest.mark.parametrize(
    ("argv", "first_path", "second_path"),
    [
        (
            ["tasks", "--state", "running"],
            "/v1/tasks?state=running",
            "/v1/tasks?state=running&cursor=NEXT",
        ),
        (["wakes"], "/v1/wakes", "/v1/wakes?cursor=NEXT"),
    ],
)
def test_list_commands_follow_every_page(
    fake: FakeCrucible,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    first_path: str,
    second_path: str,
) -> None:
    fake.responses = [
        {"schema_version": "1.0", "items": [{"id": "FIRST"}], "next_cursor": "NEXT"},
        {"schema_version": "1.0", "items": [{"id": "SECOND"}], "next_cursor": None},
    ]
    assert run(argv) == 0
    assert [path for _, path, _ in fake.calls()] == [first_path, second_path]
    data = _envelope(capsys)["data"]
    assert [item["id"] for item in data["items"]] == ["FIRST", "SECOND"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
def test_server_problem_is_exit_one_in_the_envelope_and_token_is_redacted(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    fake.status = status
    fake.response = {
        "type": "urn:crucible:problem:refused",
        "title": "Refused",
        "status": status,
        "detail": f"bad Bearer {TOKEN} and {TOKEN}",
    }
    assert run(["tasks"]) == 1
    document = _envelope(capsys)
    assert document["ok"] is False
    assert document["error"]["code"] == "refused"
    assert document["error"]["status"] == status
    assert document["error"]["problem"]["title"] == "Refused"
    assert "[REDACTED]" in document["error"]["message"]


def test_republish_404_names_missing_server_capability(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.status = 404
    fake.response = {"detail": "Not Found"}
    assert run(["republish", "T1", "--reason", "retry"]) == 1
    message = _envelope(capsys)["error"]["message"]
    assert "republish is unavailable on this Crucible server" in message


def test_unreachable_is_exit_one_in_the_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("CRUCIBLE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CRUCIBLE_TOKEN", TOKEN)
    monkeypatch.setenv("CRUCIBLE_CLIENT_CONFIG", str(tmp_path / "none.toml"))
    assert run(["tasks"]) == 1
    assert _envelope(capsys)["error"]["code"] == "unreachable"


@pytest.mark.parametrize("missing", ["CRUCIBLE_URL", "CRUCIBLE_TOKEN"])
def test_missing_configuration_is_usage_exit_two(
    fake: FakeCrucible,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    assert run(["tasks"]) == 2
    error = _envelope(capsys)["error"]
    assert error["code"] == "config" and missing in error["message"]


@pytest.mark.parametrize("url", ["not-a-url", "https://user:secret@crucible.example"])
def test_invalid_base_url_is_usage_exit_two(
    fake: FakeCrucible,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    monkeypatch.setenv("CRUCIBLE_URL", url)
    assert run(["health"]) == 2
    assert _envelope(capsys)["error"]["code"] == "config"


def test_the_flag_beats_the_environment_beats_the_file(
    fake: FakeCrucible, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    config = tmp_path / "client.toml"
    config.write_text(
        f'url = "http://127.0.0.1:1"\ntoken_file = "{token_file}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CRUCIBLE_CLIENT_CONFIG", str(config))
    monkeypatch.delenv("CRUCIBLE_TOKEN")
    # The environment's URL beats the file's; the file's token is used when no variable is.
    assert run(["health"]) == 0
    monkeypatch.setenv("CRUCIBLE_URL", "http://127.0.0.1:1")
    # The flag beats the environment.
    assert run(["--api-url", fake.url, "health"]) == 0
    assert [path for _, path, _ in fake.calls()] == ["/v1/health", "/v1/health"]


def test_table_converts_timestamps_to_chicago(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.response = {
        "schema_version": "1.0",
        "items": [
            {
                "id": "T1",
                "state": "running",
                "updated_at": "2026-09-21T16:00:00+00:00",
                "title": "t",
            }
        ],
        "next_cursor": None,
    }
    assert run(["tasks", "--table"]) == 0
    output = capsys.readouterr().out
    assert "2026-09-21 11:00:00 AM CDT" in output
    assert "+00:00" not in output


def test_token_is_redacted_even_from_success_response(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.response = {"schema_version": "1.0", "status": "ok", "detail": f"leaked {TOKEN}"}
    assert run(["health"]) == 0
    assert "[REDACTED]" in _envelope(capsys)["data"]["detail"]


def test_json_escaped_token_is_redacted_from_error_response(
    fake: FakeCrucible, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = 'abc"def\\ghi\t'
    monkeypatch.setenv("CRUCIBLE_TOKEN", token)
    fake.status = 400
    fake.response = {"type": "refused", "detail": f"echo {token}"}
    assert run(["health"]) == 1
    output = capsys.readouterr().out
    assert token not in output
    assert json.dumps(token)[1:-1] not in output
    assert "[REDACTED]" in output


def test_problem_document_with_success_status_is_refused(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.response = {"type": "unauthorized", "detail": "denied"}
    assert run(["health"]) == 1
    error = _envelope(capsys)["error"]
    assert error["code"] == "protocol" and "2xx" in error["message"]


def test_redirect_does_not_forward_token(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = FakeCrucible()
    try:
        fake.status = 302
        fake.response_headers["Location"] = f"{destination.url}/v1/health"
        assert run(["health"]) == 1
        assert not destination.requests
        assert _envelope(capsys)["ok"] is False
    finally:
        destination.close()
