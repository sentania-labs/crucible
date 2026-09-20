"""`crucible-admin` (25): every row of the operations table, through the same application
services the API calls. Local mode runs them in process against the configured
database and daemon; `--api-url` with a token runs the same operations against a
running API. Results go to stdout as JSON; logs to stderr.

Two gaps are deliberate and named here rather than implied. 25 lists four CLI-only
operations; `migrate` and `token create` are below, and the bootstrap `import` and
`export` of 15 and 14 are not implemented in this phase (c5.md C5b). And `credentials
login` runs the harness's own CLI, so it works only where that CLI is installed: local
mode on a host that has it. The Crucible service image carries none of the three, so the
API form of login refuses there with that reason rather than hanging.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from crucible.adapters.persistence.migrate import head_revision, upgrade
from crucible.application.admin import audit, credentials, github, harnesses, images, login
from crucible.application.admin import providers as providers_admin
from crucible.application.admin import repositories as repositories_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import LoginRegistry
from crucible.application.auth import mint_token
from crucible.application.errors import ApplicationError
from crucible.application.transitions import record_event
from crucible.cli.wiring import Wiring, wire
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Role
from crucible.domain.events import EventKind
from crucible.logs import configure_logging
from crucible.settings import load_settings

CLI_PRINCIPAL = "crucible-admin"
TOKEN_ENV = "CRUCIBLE_ADMIN_TOKEN"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible-admin")
    parser.add_argument("--config", default=None, help="TOML configuration file")
    parser.add_argument(
        "--api-url",
        default=None,
        help=f"remote mode: the API base (token from {TOKEN_ENV}); default runs in process",
    )
    parser.add_argument("--reason", default=None, help="the reason recorded on a mutation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply migrations to head")
    sub.add_parser("status", help="the status document (25)")

    token = sub.add_parser("token", help="token management")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="create a principal and print its token once")
    create.add_argument("--principal", required=True)
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument("--rotate", action="store_true", help="replace an existing token")

    for name in ("repository", "repositories"):
        repo = sub.add_parser(name, help="repository registry")
        repo_sub = repo.add_subparsers(dest="repo_command", required=True)
        register = repo_sub.add_parser("register")
        register.add_argument("--name", required=True)
        register.add_argument("--url", required=True)
        register.add_argument("--default-branch", default="main")
        register.add_argument("--policy", default="default-software")
        register.add_argument("--installation-id", type=int, default=None)
        register.add_argument("--attest-external-review-all-prs", action="store_true")
        register.add_argument("--attested-by", default=None)

    h = sub.add_parser("harnesses", help="list, enable, disable")
    h_sub = h.add_subparsers(dest="harness_command", required=True)
    h_sub.add_parser("list")
    for verb in ("enable", "disable"):
        p = h_sub.add_parser(verb)
        p.add_argument("name")

    c = sub.add_parser("credentials", help="validate, probe, login, rotate, remove")
    c_sub = c.add_subparsers(dest="credential_command", required=True)
    for verb in ("status", "validate", "probe", "remove"):
        p = c_sub.add_parser(verb)
        p.add_argument("--harness", required=True)
    login_cmd = c_sub.add_parser(
        "login",
        help="run the harness's own login (needs that CLI on this host; see the module docstring)",
    )
    login_cmd.add_argument("--harness", required=True)
    login_cmd.add_argument(
        "--replace",
        action="store_true",
        help="retain and shred the existing credential first; refused without it when one is valid",
    )
    rotate = c_sub.add_parser("rotate")
    rotate.add_argument("--harness", required=True)
    rotate.add_argument(
        "--new-path",
        required=True,
        help="a prepared directory to copy in; it is left untouched and is yours to dispose of",
    )

    i = sub.add_parser("images", help="list and promote")
    i_sub = i.add_subparsers(dest="image_command", required=True)
    i_sub.add_parser("list")
    promote = i_sub.add_parser("promote")
    promote.add_argument("digest")

    pr = sub.add_parser("providers")
    pr_sub = pr.add_subparsers(dest="provider_command", required=True)
    pr_sub.add_parser("status")

    g = sub.add_parser("github")
    g_sub = g.add_subparsers(dest="github_command", required=True)
    g_sub.add_parser("status")
    g_sub.add_parser("check")

    a = sub.add_parser("audit")
    a_sub = a.add_subparsers(dest="audit_command", required=True)
    tail = a_sub.add_parser("tail")
    tail.add_argument("--cursor", type=int, default=None)
    tail.add_argument("--limit", type=int, default=50)
    return parser


def _emit(document: Any) -> None:
    print(json.dumps(document, sort_keys=True, default=str))


# ----- remote mode -------------------------------------------------------------


class Remote:
    """The same operations against a running API, so a machine without the database
    or the daemon administers through the one surface (25)."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            print(raw, file=sys.stderr)
            sys.exit(2)


def _remote(args: argparse.Namespace, remote: Remote) -> None:
    reason = {"reason": args.reason or ""}
    command = args.command
    if command == "status":
        _emit(remote.call("GET", "/v1/admin/status"))
    elif command == "harnesses":
        if args.harness_command == "list":
            _emit(remote.call("GET", "/v1/admin/harnesses"))
        else:
            _emit(
                remote.call(
                    "POST", f"/v1/admin/harnesses/{args.name}/{args.harness_command}", reason
                )
            )
    elif command == "credentials":
        verb = args.credential_command
        if verb == "status":
            _emit(remote.call("GET", f"/v1/admin/credentials/{args.harness}"))
        elif verb == "rotate":
            _emit(
                remote.call(
                    "POST",
                    f"/v1/admin/credentials/{args.harness}/rotate",
                    {**reason, "new_path": args.new_path},
                )
            )
        elif verb == "login":
            _remote_login(args, remote, reason)
        else:
            _emit(remote.call("POST", f"/v1/admin/credentials/{args.harness}/{verb}", reason))
    elif command == "images":
        if args.image_command == "list":
            _emit(remote.call("GET", "/v1/admin/images"))
        else:
            _emit(remote.call("POST", f"/v1/admin/images/{args.digest}/promote", reason))
    elif command == "providers":
        _emit(remote.call("GET", "/v1/admin/providers"))
    elif command == "github":
        if args.github_command == "status":
            _emit(remote.call("GET", "/v1/admin/github"))
        else:
            _emit(remote.call("POST", "/v1/admin/github/check", reason))
    elif command == "audit":
        query = f"?limit={args.limit}" + (f"&cursor={args.cursor}" if args.cursor else "")
        _emit(remote.call("GET", "/v1/admin/audit" + query))
    elif command in ("repository", "repositories"):
        _emit(
            remote.call(
                "PUT",
                f"/v1/admin/repositories/{args.name}",
                {
                    "url": args.url,
                    "default_branch": args.default_branch,
                    "policy_name": args.policy,
                    "installation_id": args.installation_id,
                    "attested_all_prs": args.attest_external_review_all_prs,
                    "attested_by": args.attested_by,
                },
            )
        )
    else:
        print(f"{command} is CLI-only and runs in local mode", file=sys.stderr)
        sys.exit(2)


def _remote_login(args: argparse.Namespace, remote: Remote, reason: dict[str, str]) -> None:
    started = remote.call(
        "POST",
        f"/v1/admin/credentials/{args.harness}/login",
        {**reason, "replace": bool(getattr(args, "replace", False))},
    )
    print(started["window"], file=sys.stderr)
    shown: set[str] = set()
    while True:
        state = remote.call("GET", f"/v1/admin/credentials/{args.harness}/login")
        for line in state.get("output_tail", []):
            if line not in shown:
                shown.add(line)
                print(line, file=sys.stderr)
        if state["state"] == "waiting_for_code":
            code = input("paste the code: ")
            remote.call("POST", f"/v1/admin/credentials/{args.harness}/login/code", {"code": code})
        elif state["state"] in ("finished", "failed"):
            break
        time.sleep(1)
    _emit(remote.call("POST", f"/v1/admin/credentials/{args.harness}/login/finish", reason))


# ----- local mode --------------------------------------------------------------


def _local_login(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> None:
    registry = LoginRegistry()
    with wiring.ctx.uow_factory() as uow:
        started = login.start_login(
            admin,
            uow,
            registry,
            principal=CLI_PRINCIPAL,
            harness=args.harness,
            reason=args.reason,
            replace=getattr(args, "replace", False),
        )
        uow.commit()
    print(started["window"], file=sys.stderr)
    session = registry.get(args.harness)
    assert session is not None
    shown = 0
    while session.state not in ("finished", "failed"):
        for line in session.lines[shown:]:
            print(line, file=sys.stderr)
        shown = len(session.lines)
        if session.state == "waiting_for_code":
            session.submit_code(input("paste the code: "))
        time.sleep(0.5)
    for line in session.lines[shown:]:
        print(line, file=sys.stderr)
    with wiring.ctx.uow_factory() as uow:
        result = login.finish_login(
            admin, uow, registry, principal=CLI_PRINCIPAL, harness=args.harness, reason=args.reason
        )
        uow.commit()
    _emit(result)


def _local(args: argparse.Namespace, wiring: Wiring) -> None:
    admin = wiring.admin
    if admin is None:
        print("the administrative surface is not configured", file=sys.stderr)
        sys.exit(2)
    principal = CLI_PRINCIPAL
    command = args.command
    if command == "status":
        with wiring.ctx.uow_factory() as uow:
            _emit(asyncio.run(status_admin.status(admin, uow)))
    elif command == "harnesses":
        with wiring.ctx.uow_factory() as uow:
            if args.harness_command == "list":
                found = asyncio.run(harnesses.list_images(admin))
                _emit({"items": harnesses.list_harnesses(admin, uow, [i for _, i in found])})
            else:
                _emit(
                    harnesses.set_enabled(
                        admin,
                        uow,
                        principal=principal,
                        harness=args.name,
                        enabled=args.harness_command == "enable",
                        reason=args.reason,
                    )
                )
                uow.commit()
    elif command == "credentials":
        verb = args.credential_command
        if verb == "login":
            _local_login(args, wiring, admin)
            return
        with wiring.ctx.uow_factory() as uow:
            if verb == "status":
                _emit(credentials.state_view(admin, uow, args.harness))
            elif verb == "validate":
                _emit(
                    asyncio.run(
                        credentials.validate(
                            admin,
                            uow,
                            principal=principal,
                            harness=args.harness,
                            reason=args.reason,
                        )
                    ).as_dict()
                )
            elif verb == "probe":
                _emit(
                    asyncio.run(
                        credentials.probe(
                            admin,
                            uow,
                            principal=principal,
                            harness=args.harness,
                            reason=args.reason,
                        )
                    ).as_dict()
                )
            elif verb == "rotate":
                _emit(
                    credentials.rotate(
                        admin,
                        uow,
                        principal=principal,
                        harness=args.harness,
                        new_path=args.new_path,
                        reason=args.reason,
                    ).as_dict()
                )
            elif verb == "remove":
                _emit(
                    credentials.remove(
                        admin, uow, principal=principal, harness=args.harness, reason=args.reason
                    ).as_dict()
                )
            uow.commit()
    elif command == "images":
        with wiring.ctx.uow_factory() as uow:
            if args.image_command == "list":
                _emit({"items": asyncio.run(images.list_all(admin, uow))})
            else:
                _emit(
                    asyncio.run(
                        images.promote(
                            admin, uow, principal=principal, digest=args.digest, reason=args.reason
                        )
                    )
                )
                uow.commit()
    elif command == "providers":
        _emit({"items": asyncio.run(providers_admin.providers_status(admin))})
    elif command == "github":
        with wiring.ctx.uow_factory() as uow:
            if args.github_command == "status":
                _emit(github.status(admin, uow))
            else:
                _emit(github.check(admin, uow, principal=principal, reason=args.reason))
                uow.commit()
    elif command == "audit":
        with wiring.ctx.uow_factory() as uow:
            _emit(audit.tail(uow, cursor=args.cursor, limit=args.limit))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.api_url:
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            print(f"set {TOKEN_ENV} for remote mode", file=sys.stderr)
            sys.exit(2)
        _remote(args, Remote(args.api_url, token))
        return
    settings = load_settings(args.config)
    # Results go to stdout as JSON; logs go to stderr so callers can parse stdout.
    configure_logging(settings.service.log_level, stream=sys.stderr)
    if args.command == "migrate":
        upgrade(settings.database.url)
        _emit({"migrated_to": head_revision(settings.database.url)})
        return
    wiring = wire(settings)
    try:
        if args.command == "token":
            _token(args, wiring)
        elif args.command in ("repository", "repositories"):
            if wiring.admin is None:
                print("the administrative surface is not configured", file=sys.stderr)
                sys.exit(2)
            _register(args, wiring, wiring.admin)
        else:
            _local(args, wiring)
    except ApplicationError as exc:
        print(
            json.dumps({"error": exc.slug, "detail": exc.detail, "errors": exc.errors}),
            file=sys.stderr,
        )
        sys.exit(1)


def _token(args: argparse.Namespace, wiring: Wiring) -> None:
    with wiring.ctx.uow_factory() as uow:
        try:
            minted = mint_token(
                uow, wiring.ctx.clock, name=args.principal, role=Role(args.role), rotate=args.rotate
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        record_event(
            uow,
            wiring.ctx.clock,
            EventKind.PRINCIPAL_CREATED,
            principal=CLI_PRINCIPAL,
            payload={
                "principal": minted.principal.name,
                "role": minted.principal.role.value,
                "rotated": args.rotate,
            },
        )
        uow.commit()
    # The token is printed exactly once and never stored in clear.
    _emit(
        {
            "principal": minted.principal.name,
            "role": minted.principal.role.value,
            "token": minted.token,
        }
    )


def _register(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> None:
    """The same guarded service the API route calls, returning the same document."""
    with wiring.ctx.uow_factory() as uow:
        result = repositories_admin.register(
            admin,
            uow,
            principal=CLI_PRINCIPAL,
            name=args.name,
            registration=RepositoryRegistration(
                url=args.url,
                default_branch=args.default_branch,
                policy_name=args.policy,
                installation_id=args.installation_id,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=args.attest_external_review_all_prs,
                    attested_by=args.attested_by,
                ),
            ),
            reason=args.reason,
        )
        uow.commit()
    _emit(result)


if __name__ == "__main__":
    main()
