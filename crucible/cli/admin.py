"""`crucible-admin`: migrate, token create, repository register."""

from __future__ import annotations

import argparse
import json
import sys

from crucible.adapters.persistence.migrate import head_revision, upgrade
from crucible.application.auth import mint_token
from crucible.application.repositories import register_repository
from crucible.application.transitions import record_event
from crucible.cli.wiring import wire
from crucible.contracts.api import RepositoryRegistration
from crucible.domain.entities import Role
from crucible.domain.events import EventKind
from crucible.logs import configure_logging
from crucible.settings import load_settings

CLI_PRINCIPAL = "crucible-admin"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible-admin")
    parser.add_argument("--config", default=None, help="TOML configuration file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply migrations to head")

    token = sub.add_parser("token", help="token management")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="create a principal and print its token once")
    create.add_argument("--principal", required=True)
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument(
        "--rotate", action="store_true", help="replace an existing principal's token"
    )

    repo = sub.add_parser("repository", help="repository registry")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    register = repo_sub.add_parser("register")
    register.add_argument("--name", required=True)
    register.add_argument("--url", required=True)
    register.add_argument("--default-branch", default="main")
    register.add_argument("--policy", default="default-software")
    register.add_argument("--installation-id", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    configure_logging(settings.service.log_level)
    if args.command == "migrate":
        upgrade(settings.database.url)
        print(json.dumps({"migrated_to": head_revision(settings.database.url)}))
        return
    wiring = wire(settings)
    if args.command == "token":
        with wiring.ctx.uow_factory() as uow:
            try:
                minted = mint_token(
                    uow,
                    wiring.ctx.clock,
                    name=args.principal,
                    role=Role(args.role),
                    rotate=args.rotate,
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
        print(
            json.dumps(
                {
                    "principal": minted.principal.name,
                    "role": minted.principal.role.value,
                    "token": minted.token,
                }
            )
        )
        return
    if args.command == "repository":
        with wiring.ctx.uow_factory() as uow:
            repo = register_repository(
                uow,
                wiring.ctx.clock,
                principal_name=CLI_PRINCIPAL,
                name=args.name,
                registration=RepositoryRegistration(
                    url=args.url,
                    default_branch=args.default_branch,
                    policy_name=args.policy,
                    installation_id=args.installation_id,
                ),
            )
        print(json.dumps({"repository": repo.name, "id": repo.id, "url": repo.url}))
        return


if __name__ == "__main__":
    main()
