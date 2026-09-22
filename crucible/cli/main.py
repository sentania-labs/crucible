"""`crucible`: the one command. `serve` runs the service; `admin` is the operator's
console (25); the orchestrator verbs (04) are Foundry's client. Every command but
`serve` prints one JSON envelope (docs/client.md).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from crucible.cli import admin, serve
from crucible.client import orchestrator, schema
from crucible.client.config import (
    ADMIN_TOKEN_ENV,
    DEFAULT_TIMEZONE,
    TOKEN_ENV,
    read_file,
    require_remote,
    resolve,
    resolve_zone,
)
from crucible.client.envelope import (
    EXIT_OK,
    ClientError,
    Result,
    UsageError,
    dumps,
    failure,
    redact,
    success,
)
from crucible.client.http import Api
from crucible.client.table import render

log = logging.getLogger("crucible.cli")

DESCRIPTION = """\
One command for Crucible: `serve` runs the service, `admin` is the operator's console,
and the remaining verbs are the orchestrator's client of /v1.

Output. Every command except `serve` and `--help` prints exactly one JSON object on
stdout, the envelope:
  ok              true or false
  kind            names the shape of `data` (`crucible schema` prints every one)
  state           the record's lifecycle state, where it has one
  principal_role  the role of the token in use, as the API showed it, or null
  data            the API's record exactly as the API returned it
  next            actions valid from this state for this principal: each has
                  `command` (argv, `{name}` tokens to fill), `needs` (what each token
                  must be), `optional` flags, and `requires` (roles, and the task's owner)
  warnings        strings
  error           on failure: code, message, hint, and the API's problem detail
Exit code 0 when ok, 1 when the operation was refused or failed, 2 on usage.
--table (anywhere on the line) prints a short human view instead, times in local time.

Configuration, highest first: the flag, the environment, the client configuration file
($CRUCIBLE_CLIENT_CONFIG, else ~/.config/crucible/client.toml, keys url, token_file,
timezone).
  base URL   --api-url, CRUCIBLE_URL, url
  token      CRUCIBLE_TOKEN, token_file (never a flag); `admin` reads
             CRUCIBLE_ADMIN_TOKEN first
  time zone  --timezone, CRUCIBLE_TIMEZONE, timezone, America/Chicago

The token decides what the API allows and what `next` offers; the client never refuses
on its own. docs/client.md is the full reference."""

VERB_HELP = {
    "tasks": "list tasks (every page)",
    "task": "show a task and selected related records",
    "wakes": "list pending wakes, or `wakes ack ID` to acknowledge one",
    "submit": "submit a TaskContractV1 JSON document",
    "start": "start a submitted task",
    "accept": "record the acceptance decision",
    "review": "request review using a ReviewRequest JSON document",
    "dispositions": "record a ReviewDisposition JSON document",
    "corrections": "attach a correction JSON document",
    "ci-decision": "record the CI decision",
    "head-decision": "record the divergent-head decision",
    "decisions": "record a decision (answers an escalation with --escalation-id)",
    "cancel": "cancel a task",
    "close": "close a completed task",
    "republish": "retry publication after a failure",
    "health": "check Crucible liveness",
}


class Parser(argparse.ArgumentParser):
    """Usage errors become an envelope rather than argparse's prose on stderr."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message}", hint=f"run `{self.prog} --help`")


def _reason(parser: argparse.ArgumentParser, text: str = "why this is being done") -> None:
    parser.add_argument(
        "--reason",
        required=True,
        help=f"{text}; sent as X-Foundry-Reason and in the body's reason field",
    )


def _file(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("file", type=Path, metavar="FILE", help=f"path to {what}")


def _client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url",
        dest="root_api_url",
        metavar="URL",
        default=None,
        help="the API base URL (else CRUCIBLE_URL, else the file); before the verb",
    )
    parser.add_argument(
        "--timezone", default=None, help="the zone --table renders times in (America/Chicago)"
    )
    parser.add_argument("--table", action="store_true", help="a short human view")


def _add_orchestrator_verbs(sub: Any) -> None:
    def verb(name: str) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = sub.add_parser(
            name, help=VERB_HELP[name], description=VERB_HELP[name]
        )
        return parser

    tasks = verb("tasks")
    tasks.add_argument("--state", help="only tasks in this state")

    task = verb("task")
    task.add_argument("id", help="the task id")
    task.add_argument("--events", action="store_true", help="include every event")
    task.add_argument("--pull-request", action="store_true", help="include the pull request")
    task.add_argument("--attempts", action="store_true", help="include every attempt")
    task.add_argument("--gates", action="store_true", help="the latest attempt's gates")
    task.add_argument("--report", action="store_true", help="the latest attempt's report")
    task.add_argument("--evidence", action="store_true", help="the latest attempt's evidence")

    wakes = verb("wakes")
    wakes_sub = wakes.add_subparsers(dest="wakes_command")
    ack = wakes_sub.add_parser("ack", help="acknowledge a wake")
    ack.add_argument("id", help="the wake id")
    _reason(ack, "what was done about the wake; sent as the ack note")

    submit = verb("submit")
    _file(submit, "a TaskContractV1 JSON document")
    _reason(submit)

    start = verb("start")
    start.add_argument("id", help="the task id")
    start.add_argument(
        "--policy-version", required=True, type=int, help="the contract's policy version"
    )
    for name in ("harness", "model", "provider", "image", "effort"):
        start.add_argument(f"--{name}", help="must equal the contract's value when given")
    _reason(start)

    accept = verb("accept")
    accept.add_argument("id", help="the task id")
    accept.add_argument(
        "--verdict", required=True, choices=("accepted", "rejected", "needs_more_work")
    )
    accept.add_argument("--head-sha", help="the head the verdict is for")
    _reason(accept, "the reasoning for the verdict")

    review = verb("review")
    review.add_argument("id", help="the task id")
    _file(review, "a ReviewRequest JSON document")
    _reason(review)

    for name, what in (
        ("dispositions", "a ReviewDisposition JSON document"),
        ("corrections", "a correction JSON document"),
    ):
        command = verb(name)
        command.add_argument("id", help="the task id")
        _file(command, what)
        _reason(command)

    ci = verb("ci-decision")
    ci.add_argument("id", help="the task id")
    ci.add_argument("--cause", required=True, help="the CI failure cause (see `next`)")
    ci.add_argument("--action", required=True, help="rerun, correct, reject, or cancel")
    _reason(ci, "the reasoning for the decision")

    head = verb("head-decision")
    head.add_argument("id", help="the task id")
    head.add_argument("--action", required=True, help="recollect, reject, or cancel")
    _reason(head, "the reasoning for the decision")

    decisions = verb("decisions")
    decisions.add_argument("id", help="the task id")
    decisions.add_argument("--kind", required=True, help="the decision kind")
    decisions.add_argument("--verbatim", required=True, help="the deciding person's words")
    decisions.add_argument("--resolves", required=True, help="what the decision resolves")
    decisions.add_argument("--escalation-id", help="the open escalation this answers")
    decisions.add_argument(
        "--reschedule", action="store_true", help="send a blocked task back to work"
    )
    _reason(decisions)

    cancel = verb("cancel")
    cancel.add_argument("id", help="the task id")
    cancel.add_argument("--verbatim", required=True, help="the deciding person's words")
    cancel.add_argument("--decided-by", default="foundry", help="who decided (foundry)")
    _reason(cancel)

    close = verb("close")
    close.add_argument("id", help="the task id")
    _reason(close, "the closing note")

    republish = verb("republish")
    republish.add_argument("id", help="the task id")
    _reason(republish)

    verb("health")


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="crucible",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _client_options(parser)
    # `group`, not `command`: the admin group's own verbs are parsed into `command`.
    sub = parser.add_subparsers(dest="group", required=True)
    sub.add_parser(
        "serve",
        help="run the api and/or the supervisor (prints logs, not an envelope)",
        add_help=False,
    )
    admin.build_parser(sub.add_parser("admin", help="the operator's console (25)"))
    sub.add_parser(
        "schema",
        help="print the envelope's JSON schema and every kind's",
        description=("The envelope's JSON schema and the schema of `data` for every `kind`."),
    )
    _add_orchestrator_verbs(sub)
    return parser


def _split_table(argv: list[str]) -> tuple[list[str], bool]:
    """`--table` anywhere on the line, as foundry-crucible took it."""
    table = "--table" in argv
    return [arg for arg in argv if arg != "--table"], table


def _prefix(args: argparse.Namespace) -> list[str]:
    return ["crucible", *(["--api-url", args.root_api_url] if args.root_api_url else [])]


def _execute(args: argparse.Namespace) -> Result:
    if args.group == "schema":
        return Result(kind="schema", data=schema.document())
    if args.group == "admin":
        return admin.run(args, root_api_url=args.root_api_url, timezone=args.timezone)
    args.command = args.group
    config = resolve(api_url=args.root_api_url, timezone=args.timezone)
    base_url, token = require_remote(config, (TOKEN_ENV,))
    return orchestrator.run(Api(base_url, token), args, _prefix(args))


def _secrets() -> list[str]:
    """The bearer tokens in the environment, redacted from any envelope. A value too short
    to be a token would redact ordinary text, so it is not treated as one."""
    values = (os.environ.get(name, "").strip() for name in (TOKEN_ENV, ADMIN_TOKEN_ENV))
    return [value for value in values if len(value) >= 16]


def _zone(timezone: str | None) -> ZoneInfo:
    try:
        return resolve_zone(timezone, os.environ, read_file(os.environ))
    except UsageError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def run(argv: Sequence[str] | None = None) -> int:
    """Parse, run, print one envelope; the exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["serve"]:
        serve.main(raw)
        return EXIT_OK
    normalized, table = _split_table(raw)
    envelope: dict[str, Any]
    code = EXIT_OK
    timezone: str | None = None
    try:
        args = build_parser().parse_args(normalized)
        timezone = args.timezone
        table = table or args.table
        envelope = success(_execute(args))
    except ClientError as exc:
        envelope, code = failure(exc), exc.exit_code
    except KeyboardInterrupt:
        envelope = failure(ClientError("interrupted", "interrupted", hint="run it again"))
        code = 1
    except Exception as exc:  # the envelope, never a bare traceback on stdout
        traceback.print_exc(file=sys.stderr)
        envelope = failure(
            ClientError(
                "internal",
                f"{type(exc).__name__}: {exc}",
                hint="the traceback is on stderr; this is a defect or an environment fault",
            )
        )
        code = 1
    envelope = redact(envelope, _secrets())
    if table:
        render(envelope, _zone(timezone), sys.stdout, sys.stderr)
    else:
        print(dumps(envelope))
    return code


def main(argv: Sequence[str] | None = None) -> NoReturn:
    sys.exit(run(argv))


def admin_shim(argv: Sequence[str] | None = None) -> NoReturn:
    """`crucible-admin`: the same group, behind one line of deprecation on stderr."""
    print("crucible-admin is deprecated; use `crucible admin` (same arguments)", file=sys.stderr)
    raw = list(sys.argv[1:] if argv is None else argv)
    sys.exit(run(["admin", *raw]))
