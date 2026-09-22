"""The one JSON object every `crucible` command prints (docs/client.md).

`ok`, `kind`, `data`, `state`, `next`, `warnings`, and on failure `error`. `data` is the
API's record exactly as the API returned it; nothing here rewords it. Exit code 0 on
`ok`, 1 on a refused or failed operation, 2 on usage.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

ENVELOPE_VERSION = "1"

# What to do next about an error, by code. The message says what happened; the hint says
# what the caller can do about it. A code missing here gets no hint rather than a guess.
HINTS: dict[str, str] = {
    "usage": "run the command with --help for its arguments",
    "config": "see `crucible --help` for where the base URL and token come from",
    "unauthorized": "the token was not accepted; check CRUCIBLE_TOKEN (or "
    "CRUCIBLE_ADMIN_TOKEN for `crucible admin`) holds a current token",
    "forbidden": "the token's principal lacks the role this operation needs, or the task "
    "belongs to another principal; use a token with the right role",
    "not-found": "check the identifier; list the records to find it",
    "transition-not-allowed": "the record's state does not permit this; read it again and "
    "follow its `next` actions",
    "supervisor-not-live": "mutations need a live supervisor lease; start the supervisor "
    "(`crucible serve --supervisor` or `--all`) and retry",
    "request-invalid": "see error.problem.errors for the fields that failed",
    "contract-invalid": "see error.problem.errors for the fields that failed",
    "conflict": "the request conflicts with the record's current state; read it again",
    "idempotency-in-progress": "the same request is still running; retry shortly",
    "unreachable": "check the base URL and that the Crucible API is running",
    "protocol": "the server answered outside its contract; check the base URL points at a "
    "Crucible API of a matching version",
    "unsupported": "the server does not offer this operation; it may be older than this client",
}


class ClientError(Exception):
    """A refused or failed operation, carried to the envelope's `error`."""

    exit_code = EXIT_FAILED

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        problem: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else HINTS.get(code)
        self.problem = problem
        self.status = status

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.status is not None:
            error["status"] = self.status
        if self.problem is not None:
            error["problem"] = self.problem
        return error


class UsageError(ClientError):
    """Invalid input the command line could not express or the caller got wrong."""

    exit_code = EXIT_USAGE

    def __init__(self, message: str, *, hint: str | None = None, code: str = "usage") -> None:
        super().__init__(code, message, hint=hint)


def problem_code(problem: Any, status: int) -> str:
    """The error code of an RFC 9457 problem: the slug of its `type` URI."""
    if isinstance(problem, dict) and isinstance(problem.get("type"), str):
        slug = str(problem["type"]).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if slug:
            return slug
    return f"http-{status}"


def refusal(status: int, problem: Any, raw: str) -> ClientError:
    """The API's refusal as a ClientError, its problem detail carried whole."""
    code = problem_code(problem, status)
    if isinstance(problem, dict):
        message = str(problem.get("detail") or problem.get("title") or raw or f"HTTP {status}")
        return ClientError(code, message, problem=problem, status=status)
    return ClientError(code, raw or f"HTTP {status}", status=status)


@dataclass
class Result:
    """A successful command's output, before it becomes an envelope."""

    kind: str
    data: Any
    state: str | None = None
    next: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    role: str | None = None


def success(result: Result) -> dict[str, Any]:
    return {
        "ok": True,
        "envelope": ENVELOPE_VERSION,
        "kind": result.kind,
        "state": result.state,
        "principal_role": result.role,
        "data": result.data,
        "next": result.next,
        "warnings": result.warnings,
    }


def failure(
    error: ClientError, *, kind: str | None = None, warnings: Iterable[str] = ()
) -> dict[str, Any]:
    return {
        "ok": False,
        "envelope": ENVELOPE_VERSION,
        "kind": kind or "error",
        "state": None,
        "principal_role": None,
        "data": None,
        "next": [],
        "warnings": list(warnings),
        "error": error.as_error(),
    }


# ----- redaction ---------------------------------------------------------------------


def _forms(secret: str) -> list[str]:
    forms = {
        secret,
        json.dumps(secret, ensure_ascii=False)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret, safe=""),
    }
    return sorted((form for form in forms if form), key=len, reverse=True)


def redact_text(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if not secret:
            continue
        for form in _forms(secret):
            text = text.replace(form, "[REDACTED]")
    return text


def redact(document: Any, secrets: Iterable[str]) -> Any:
    """Every string in the document with the bearer token in use removed, in each form a
    server or a library might have echoed it. Only the token in use: a token the command
    was asked to mint (`admin token create`) is its output, printed once."""
    values = [secret for secret in secrets if secret]
    if not values:
        return document
    if isinstance(document, str):
        return redact_text(document, values)
    if isinstance(document, list):
        return [redact(item, values) for item in document]
    if isinstance(document, dict):
        return {redact_text(str(k), values): redact(v, values) for k, v in document.items()}
    return document


def dumps(envelope: dict[str, Any]) -> str:
    """One line of JSON: a caller reads stdout whole, or its last line."""
    return json.dumps(envelope, default=str, separators=(",", ":"))
