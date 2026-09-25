"""`crucible admin` (25): every row of the operations table, through the same application
services the API calls. Local mode runs them in process against the configured
database and daemon; `--api-url` (or `--remote`) with a token runs the same operations
against a running API. Each prints one envelope (docs/client.md) on stdout; logs and the
interactive parts of a login go to stderr. `crucible-admin` is the same group behind a
deprecation line.

Two gaps are deliberate and named here rather than implied. 25 lists four CLI-only
operations; `migrate` and `token create` are below, the bootstrap import of 15 is the
`bootstrap` group below (C6, with its API under `/v1/import/bootstrap`), and the
portable `export` of 14 is not implemented yet. `credentials login` runs the harness's
own CLI: in local mode, directly on this host, and it needs that CLI installed there;
remotely, the API runs it in the promoted worker image and refuses with a clear reason
when none is available, rather than hanging.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import time
import urllib.parse
from typing import Any

from crucible.adapters.clock import SystemClock
from crucible.adapters.persistence.migrate import head_revision, upgrade
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.admin import (
    audit,
    bootstrap,
    credentials,
    gateway,
    github,
    harnesses,
    images,
    login,
    routing,
)
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.admin import providers as providers_admin
from crucible.application.admin import repositories as repositories_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin import (
    tokens as tokens_admin,
)
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import LoginRegistry
from crucible.application.auth import mint_token
from crucible.application.errors import ApplicationError
from crucible.application.first_run import FIRST_RUN_PREFIX
from crucible.application.queries import task_view
from crucible.application.republish import republish_task
from crucible.application.transitions import record_event
from crucible.cli.wiring import Wiring, first_run_delivery, wire
from crucible.client import next as nx
from crucible.client.config import ADMIN_TOKEN_ENV, TOKEN_ENV, require_remote, resolve
from crucible.client.envelope import ClientError, Result, UsageError
from crucible.client.http import Api
from crucible.contracts.api import (
    ExternalReviewAttestation,
    PublishRetryRequest,
    RepositoryRegistration,
)
from crucible.contracts.problem import problem_type
from crucible.domain.cluster_egress import parse_labels
from crucible.domain.entities import Principal, Role
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.logs import configure_logging
from crucible.ports.first_run import FirstRunDelivery
from crucible.settings import load_settings

CLI_PRINCIPAL = "crucible-admin"
TOKEN_ENVS = (ADMIN_TOKEN_ENV, TOKEN_ENV)

DESCRIPTION = """\
The operator's console (25): harness gates, credentials and login, image promotion,
tokens, repositories, routing, the local gateway, GitHub, the bootstrap import, audit.
Runs in process against the configured database by default; with --api-url URL (or
--remote, which takes the URL from CRUCIBLE_URL or the client configuration file) it
calls the running API with the token in CRUCIBLE_ADMIN_TOKEN, else CRUCIBLE_TOKEN.
Every mutation takes --reason, placed before the verb:
`crucible admin --reason TEXT harnesses disable codex`. Output is one JSON envelope
(see `crucible --help`)."""


def build_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The admin group's arguments, unchanged from `crucible-admin`, onto `parser`."""
    parser.description = DESCRIPTION
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument(
        "--config", default=None, help="the server's TOML configuration file (local mode)"
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="remote mode: the API base; token from CRUCIBLE_ADMIN_TOKEN, else CRUCIBLE_TOKEN",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="remote mode with the base URL from CRUCIBLE_URL or the client configuration",
    )
    parser.add_argument(
        "--reason", default=None, help="the reason recorded on a mutation (required on one)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply migrations to head (local only)")
    sub.add_parser("status", help="the sanitized status document (25)")

    task = sub.add_parser("task", help="task recovery operations")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    republish = task_sub.add_parser("republish", help="retry a failed publication")
    republish.add_argument("task_id", help="the task in publish_failed")
    republish.add_argument("--reason", required=True, help="why the retry is safe now")

    token = sub.add_parser("token", help="token management")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("list", help="list principals without token values")
    create = token_sub.add_parser("create", help="create a principal and print its token once")
    create.add_argument("--principal", required=True, help="the new principal's name")
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument("--rotate", action="store_true", help="refused: revoke and create")
    revoke = token_sub.add_parser("revoke", help="disable a principal token")
    revoke.add_argument("principal_id", help="an id from `token list`")

    for name in ("repository", "repositories"):
        repo = sub.add_parser(name, help="repository registry")
        repo_sub = repo.add_subparsers(dest="repo_command", required=True)
        repo_sub.add_parser("list", help="registered repositories")
        register = repo_sub.add_parser("register", help="register or update a repository")
        register.add_argument("--name", required=True)
        register.add_argument("--url", required=True, help="https://github.com/OWNER/REPO")
        register.add_argument("--default-branch", default="main")
        register.add_argument("--policy", default="default-software", help="the policy name")
        register.add_argument(
            "--installation-id", type=int, default=None, help="the GitHub App installation"
        )
        register.add_argument(
            "--attest-external-review-all-prs",
            action="store_true",
            help="attest the external reviewer reviews every pull request (23)",
        )
        register.add_argument("--attested-by", default=None, help="who attests")
        remove = repo_sub.add_parser("remove", help="remove a registered repository")
        remove.add_argument("name")

    h = sub.add_parser("harnesses", help="list, enable, disable")
    h_sub = h.add_subparsers(dest="harness_command", required=True)
    h_sub.add_parser("list", help="harnesses, their enable flags, credentials and images")
    for verb in ("enable", "disable"):
        p = h_sub.add_parser(verb, help=f"{verb} a harness for new launches")
        p.add_argument("name", help="claude_code, codex, agy, hermes")

    c = sub.add_parser("credentials", help="status, set, validate, probe, login, rotate, remove")
    c_sub = c.add_subparsers(dest="credential_command", required=True)
    words = {
        "status": "presence, permissions, expiry class; never a value",
        "validate": "shape check of the auth files, then the bounded probe",
        "probe": "a bounded run of the hardened image",
        "remove": "retain and shred the credential",
    }
    for verb in ("status", "validate", "probe", "remove"):
        p = c_sub.add_parser(verb, help=words[verb])
        p.add_argument("--harness", required=True)
    login_cmd = c_sub.add_parser(
        "login",
        help="run the harness's own login (locally, or in the worker image remotely; "
        "see the module docstring)",
    )
    login_cmd.add_argument("--harness", required=True)
    login_cmd.add_argument(
        "--replace",
        action="store_true",
        help="retain and shred the existing credential first; refused without it when one is valid",
    )
    rotate = c_sub.add_parser("rotate", help="copy a prepared credential directory in")
    rotate.add_argument("--harness", required=True)
    rotate.add_argument(
        "--new-path",
        required=True,
        help="a prepared directory to copy in; it is left untouched and is yours to dispose of",
    )
    set_key = c_sub.add_parser("set", help="read an API key without placing it in argv")
    set_key.add_argument("--harness", default="hermes", choices=("hermes",))

    i = sub.add_parser("images", help="list and promote")
    i_sub = i.add_subparsers(dest="image_command", required=True)
    i_sub.add_parser(
        "list", help="worker images and their promotion state (ci-* tags are not listed)"
    )
    promote = i_sub.add_parser("promote", help="promote a candidate image")
    promote.add_argument("digest", help="the image's digest or reference")

    pr = sub.add_parser("providers", help="execution providers")
    pr_sub = pr.add_subparsers(dest="provider_command", required=True)
    pr_sub.add_parser("status", help="each provider's health")

    g = sub.add_parser("github", help="the GitHub App")
    g_sub = g.add_subparsers(dest="github_command", required=True)
    g_sub.add_parser("status", help="the App's configuration and key")
    g_sub.add_parser("check", help="mint and discard a token per registered repository")
    connect = g_sub.add_parser(
        "connect",
        help="an existing App's id and private key, checked with GitHub and then kept by "
        "the service (ADR 0017)",
    )
    connect.add_argument("--app-id", type=int, required=True, help="the App's numeric id")
    connect.add_argument(
        "--private-key-file",
        required=True,
        help="the .pem GitHub gave you; read here, never placed in argv or a record",
    )
    connect.add_argument(
        "--webhook-secret-file", default=None, help="the App's webhook secret, when one is used"
    )
    g_sub.add_parser(
        "installations", help="the App's install link, installations and their repositories"
    )
    add_repo = g_sub.add_parser(
        "add-repository",
        help="register a repository an installation covers, with GitHub's default branch",
    )
    add_repo.add_argument("--installation-id", type=int, required=True)
    add_repo.add_argument("--repository", required=True, help="OWNER/NAME")
    add_repo.add_argument(
        "--name", default=None, help="the registered name (default: the repository's own)"
    )
    add_repo.add_argument("--policy", default="default-software", help="the policy name")
    add_repo.add_argument(
        "--attest-external-review-all-prs",
        action="store_true",
        help="attest the external reviewer reviews every pull request (23)",
    )
    add_repo.add_argument("--attested-by", default=None, help="who attests")

    gw = sub.add_parser(
        "gateway", help="the local gateway: its URL, the Hermes key, a test, and its models"
    )
    gw_sub = gw.add_subparsers(dest="gateway_command", required=True)
    gw_sub.add_parser("show", help="the gateway URL, whether a key is set, and the last test")
    gw_set = gw_sub.add_parser(
        "set", help="set the gateway URL (and the key with --key), then test both"
    )
    gw_set.add_argument(
        "--endpoint-url", required=True, help="the gateway's base URL, ending in /v1"
    )
    gw_set.add_argument(
        "--key",
        action="store_true",
        help="also read a new Hermes key from a hidden prompt or stdin, never argv",
    )
    gw_sub.add_parser("test", help="test the saved URL and key again")
    gw_sub.add_parser("models", help="the models the key can see, beside the entries in force")
    pick = gw_sub.add_parser(
        "pick", help="enable or disable gateway models; writes a new routing policy version"
    )
    pick.add_argument("--enable", action="append", default=[], metavar="MODEL")
    pick.add_argument("--disable", action="append", default=[], metavar="MODEL")
    pick.add_argument(
        "--thinking",
        action="append",
        default=[],
        metavar="MODEL",
        help="thinking on by default for this picked model (off for the others)",
    )
    pick.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="MODEL=CAPABILITY",
        help="small, mid or frontier (a new model defaults to mid)",
    )
    pick.add_argument("--max-concurrency", type=int, default=None, help="the pool's limit")

    a = sub.add_parser("audit", help="the audit log")
    a_sub = a.add_subparsers(dest="audit_command", required=True)
    tail = a_sub.add_parser("tail", help="administrative events, oldest first")
    tail.add_argument("--cursor", type=int, default=None, help="a next_cursor from a page")
    tail.add_argument("--limit", type=int, default=50)

    route = sub.add_parser("routing", help="local endpoint and reactive quota exhaustion")
    route_sub = route.add_subparsers(dest="routing_command", required=True)
    route_sub.add_parser("exhaustion", help="the exhaustion marks")
    clear = route_sub.add_parser("clear-exhaustion", help="clear a pool's exhaustion mark")
    clear.add_argument("pool")
    route_sub.add_parser("local-endpoint")
    local_set = route_sub.add_parser("set-local-endpoint")
    local_set.add_argument("--endpoint-url", required=True)
    local_set.add_argument("--model", default="coder")
    state = local_set.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    local_set.add_argument("--enable-thinking", action="store_true")
    local_set.add_argument("--max-concurrency", type=int, default=4)

    kube = sub.add_parser(
        "kubernetes", help="the Kubernetes provider's cluster egress selectors (26, #91)"
    )
    kube_sub = kube.add_subparsers(dest="kubernetes_command", required=True)
    kube_sub.add_parser("egress", help="the kubernetes.egress setting in force and its source")
    egress_set = kube_sub.add_parser(
        "set-egress",
        help="replace kubernetes.egress: the resolver's pods and an in-cluster local endpoint",
    )
    egress_set.add_argument(
        "--dns-namespace",
        default="kube-system",
        help="the cluster resolver's namespace; empty for its service address alone",
    )
    egress_set.add_argument(
        "--dns-labels",
        default="k8s-app=kube-dns",
        help="the resolver's pod labels, key=value[,key=value]",
    )
    egress_set.add_argument(
        "--endpoint-namespace",
        default="",
        help="an in-cluster local endpoint's namespace; empty when it is outside the cluster",
    )
    egress_set.add_argument(
        "--endpoint-labels", default="", help="its pod labels, key=value[,key=value]"
    )
    egress_set.add_argument(
        "--endpoint-port",
        type=int,
        default=0,
        help="its pods' port; 0 for the endpoint URL's own port",
    )

    b = sub.add_parser(
        "bootstrap", help="the bootstrap ledger handoff (15): submit, show, list, commit"
    )
    b_sub = b.add_subparsers(dest="bootstrap_command", required=True)
    submit = b_sub.add_parser(
        "submit",
        help="validate a BootstrapExportV1 bundle and write it as a verified import",
    )
    submit.add_argument("--file", required=True, help="the crucible.json foundry-ledger exported")
    submit.add_argument(
        "--owner",
        default=None,
        help="the principal the imported tasks belong to (default: this CLI's principal)",
    )
    show = b_sub.add_parser("show", help="the verification report of one import")
    show.add_argument("import_id")
    b_sub.add_parser("list", help="every import")
    commit = b_sub.add_parser("commit", help="make a verified import authoritative")
    commit.add_argument("import_id")
    return parser


def _read_code() -> str:
    """The login code a person pastes. The prompt goes to stderr and the code is read
    from stdin, so stdout carries the envelope alone even when stdin is not a terminal."""
    print("paste the code: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().rstrip("\r\n")


def _read_bundle(path: str) -> Any:
    """The bundle file, parsed and nothing else: validation is the service's."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read a bundle from {path}: {exc}") from None


def _not_configured() -> ClientError:
    return ClientError(
        "admin-not-configured",
        "the administrative surface is not configured",
        hint="set the [admin] section of the server configuration (25)",
    )


def application_error(exc: ApplicationError) -> ClientError:
    """A local refusal in the shape the API would have sent it (RFC 9457)."""
    problem = {
        "type": problem_type(exc.slug),
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
        "errors": exc.errors or [],
    }
    return ClientError(exc.slug, exc.detail or exc.title, problem=problem, status=exc.status)


def _egress(args: argparse.Namespace) -> dict[str, Any]:
    """`set-egress` flags as the `kubernetes.egress` document the service checks."""
    try:
        return {
            "dns": {
                "namespace": args.dns_namespace,
                "pod_labels": parse_labels(args.dns_labels),
            },
            "local_endpoint": {
                "namespace": args.endpoint_namespace,
                "pod_labels": parse_labels(args.endpoint_labels),
                "port": args.endpoint_port,
            },
        }
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _read_api_key() -> str:
    """Read a key from a hidden terminal prompt or stdin, never from argv."""
    return (
        getpass.getpass("LiteLLM virtual key: ")
        if sys.stdin.isatty()
        else sys.stdin.readline().rstrip("\r\n")
    )


def _picks(args: argparse.Namespace) -> list[dict[str, Any]]:
    """`gateway pick` flags as the model picks the service checks."""
    capabilities: dict[str, str] = {}
    for item in args.capability:
        model, sep, value = item.partition("=")
        if not sep or not model or not value:
            raise UsageError(f"--capability takes MODEL=CAPABILITY, not {item!r}")
        capabilities[model] = value
    named = list(dict.fromkeys([*args.enable, *args.disable]))
    stray = sorted((set(args.thinking) | set(capabilities)) - set(named))
    if stray:
        raise UsageError(f"{stray} must also be named with --enable or --disable")
    if not named:
        raise UsageError("name at least one model with --enable or --disable")
    return [
        {
            "id": model,
            "enabled": model in args.enable and model not in args.disable,
            "enable_thinking": model in args.thinking,
            "capability": capabilities.get(model),
        }
        for model in named
    ]


def _read_file(path: str, what: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise UsageError(f"cannot read the {what} from {path}: {exc.strerror}") from None


def _connect_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "app_id": args.app_id,
        "private_key": _read_file(args.private_key_file, "private key"),
        "webhook_secret": (
            _read_file(args.webhook_secret_file, "webhook secret")
            if args.webhook_secret_file
            else None
        ),
    }


def _add_repository_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "installation_id": args.installation_id,
        "repository": args.repository,
        "name": args.name,
        "policy_name": args.policy,
        "attested_all_prs": args.attest_external_review_all_prs,
        "attested_by": args.attested_by,
    }


# ----- remote mode -------------------------------------------------------------


def _remote(args: argparse.Namespace, remote: Api) -> Any:
    reason = {"reason": args.reason or ""}
    command = args.command
    if command == "status":
        return remote.call("GET", "/v1/admin/status")
    if command == "task":
        return remote.call("POST", f"/v1/tasks/{args.task_id}/republish", {"reason": args.reason})
    if command == "token":
        if args.token_command == "list":
            return remote.call("GET", "/v1/admin/tokens")
        if args.token_command == "revoke":
            return remote.call("POST", f"/v1/admin/tokens/{args.principal_id}/revoke", reason)
        if args.rotate:
            raise UsageError("remote token rotation is not supported; revoke and create")
        return remote.call(
            "POST", "/v1/admin/tokens", {**reason, "name": args.principal, "role": args.role}
        )
    if command == "harnesses":
        if args.harness_command == "list":
            return remote.call("GET", "/v1/admin/harnesses")
        return remote.call(
            "POST", f"/v1/admin/harnesses/{args.name}/{args.harness_command}", reason
        )
    if command == "credentials":
        verb = args.credential_command
        if verb == "status":
            return remote.call("GET", f"/v1/admin/credentials/{args.harness}")
        if verb == "set":
            return remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/set",
                {**reason, "api_key": _read_api_key()},
            )
        if verb == "rotate":
            return remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/rotate",
                {**reason, "new_path": args.new_path},
            )
        if verb == "login":
            return _remote_login(args, remote, reason)
        return remote.call("POST", f"/v1/admin/credentials/{args.harness}/{verb}", reason)
    if command == "images":
        if args.image_command == "list":
            return remote.call("GET", "/v1/admin/images")
        return remote.call("POST", f"/v1/admin/images/{args.digest}/promote", reason)
    if command == "providers":
        return remote.call("GET", "/v1/admin/providers")
    if command == "github":
        if args.github_command == "status":
            return remote.call("GET", "/v1/admin/github")
        if args.github_command == "connect":
            return remote.call("POST", "/v1/admin/github/app", {**reason, **_connect_body(args)})
        if args.github_command == "installations":
            return remote.call("GET", "/v1/admin/github/installations")
        if args.github_command == "add-repository":
            return remote.call(
                "POST", "/v1/admin/github/repositories", {**reason, **_add_repository_body(args)}
            )
        return remote.call("POST", "/v1/admin/github/check", reason)
    if command == "gateway":
        verb = args.gateway_command
        if verb == "show":
            return remote.call("GET", "/v1/admin/gateway")
        if verb == "models":
            return remote.call("GET", "/v1/admin/gateway/models")
        if verb == "test":
            return remote.call("POST", "/v1/admin/gateway/test", reason)
        if verb == "pick":
            return remote.call(
                "POST",
                "/v1/admin/gateway/models",
                {**reason, "models": _picks(args), "max_concurrency": args.max_concurrency},
            )
        return remote.call(
            "POST",
            "/v1/admin/gateway",
            {
                **reason,
                "endpoint_url": args.endpoint_url,
                "api_key": _read_api_key() if args.key else None,
            },
        )
    if command == "audit":
        query = f"?limit={args.limit}" + (f"&cursor={args.cursor}" if args.cursor else "")
        return remote.call("GET", "/v1/admin/audit" + query)
    if command == "routing":
        if args.routing_command == "exhaustion":
            return remote.call("GET", "/v1/admin/routing/exhaustion")
        if args.routing_command == "clear-exhaustion":
            return remote.call("POST", f"/v1/admin/routing/exhaustion/{args.pool}/clear", reason)
        if args.routing_command == "local-endpoint":
            return remote.call("GET", "/v1/admin/routing/local-endpoint")
        return remote.call(
            "POST",
            "/v1/admin/routing/local-endpoint",
            {
                **reason,
                "endpoint_url": args.endpoint_url,
                "models": [
                    {
                        "id": args.model,
                        "enabled": args.enable,
                        "enable_thinking": args.enable_thinking,
                    }
                ],
                "max_concurrency": args.max_concurrency,
            },
        )
    if command == "kubernetes":
        if args.kubernetes_command == "egress":
            return remote.call("GET", "/v1/admin/kubernetes/egress")
        return remote.call("POST", "/v1/admin/kubernetes/egress", {**reason, **_egress(args)})
    if command == "bootstrap":
        verb = args.bootstrap_command
        if verb == "submit":
            query = "?" + urllib.parse.urlencode(
                {k: v for k, v in (("reason", args.reason), ("owner", args.owner)) if v}
            )
            return remote.call("POST", "/v1/import/bootstrap" + query, _read_bundle(args.file))
        if verb == "show":
            return remote.call("GET", f"/v1/import/bootstrap/{args.import_id}")
        if verb == "list":
            return remote.call("GET", "/v1/import/bootstrap")
        return remote.call("POST", f"/v1/import/bootstrap/{args.import_id}/commit", reason)
    if command in ("repository", "repositories"):
        if args.repo_command == "list":
            return remote.call("GET", "/v1/admin/repositories")
        if args.repo_command == "remove":
            return remote.call("DELETE", f"/v1/admin/repositories/{args.name}", reason)
        return remote.call(
            "PUT",
            f"/v1/admin/repositories/{args.name}",
            {
                **reason,
                "url": args.url,
                "default_branch": args.default_branch,
                "policy_name": args.policy,
                "installation_id": args.installation_id,
                "attested_all_prs": args.attest_external_review_all_prs,
                "attested_by": args.attested_by,
            },
        )
    raise UsageError(f"{command} is CLI-only and runs in local mode; drop --api-url")


def _remote_login(args: argparse.Namespace, remote: Api, reason: dict[str, str]) -> Any:
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
            code = _read_code()
            remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/login/code",
                {**reason, "code": code},
            )
        elif state["state"] in ("finished", "failed"):
            break
        time.sleep(1)
    return remote.call("POST", f"/v1/admin/credentials/{args.harness}/login/finish", reason)


# ----- local mode --------------------------------------------------------------


def _local_login(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
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
            session.submit_code(_read_code())
        time.sleep(0.5)
    for line in session.lines[shown:]:
        print(line, file=sys.stderr)
    with wiring.ctx.uow_factory() as uow:
        result = login.finish_login(
            admin, uow, registry, principal=CLI_PRINCIPAL, harness=args.harness, reason=args.reason
        )
        uow.commit()
    return result


def _local(args: argparse.Namespace, wiring: Wiring) -> Any:
    admin = wiring.admin
    if admin is None:
        raise _not_configured()
    principal = CLI_PRINCIPAL
    command = args.command
    if command == "status":
        with wiring.ctx.uow_factory() as uow:
            return asyncio.run(status_admin.status(admin, uow))
    if command == "task":
        with wiring.ctx.uow_factory() as uow:
            task = republish_task(
                uow,
                wiring.ctx.clock,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                task_id=args.task_id,
                request=PublishRetryRequest(reason=args.reason),
            )
            uow.commit()
            return task_view(uow, task.id).model_dump(mode="json")
    if command == "harnesses":
        with wiring.ctx.uow_factory() as uow:
            if args.harness_command == "list":
                found = asyncio.run(harnesses.list_images(admin))
                return {"items": harnesses.list_harnesses(admin, uow, [i for _, i in found])}
            result = harnesses.set_enabled(
                admin,
                uow,
                principal=principal,
                harness=args.name,
                enabled=args.harness_command == "enable",
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "credentials":
        return _local_credentials(args, wiring, admin, principal)
    if command == "images":
        with wiring.ctx.uow_factory() as uow:
            if args.image_command == "list":
                return {"items": asyncio.run(images.list_all(admin, uow))}
            result = asyncio.run(
                images.promote(
                    admin, uow, principal=principal, digest=args.digest, reason=args.reason
                )
            )
            uow.commit()
            return result
    if command == "providers":
        return {"items": asyncio.run(providers_admin.providers_status(admin))}
    if command == "github":
        with wiring.ctx.uow_factory() as uow:
            verb = args.github_command
            if verb == "status":
                return github.status(admin, uow)
            if verb == "installations":
                return github.apps_view(admin, uow)
            if verb == "connect":
                body = _connect_body(args)
                result = github.connect(
                    admin,
                    uow,
                    principal=principal,
                    app_id=body["app_id"],
                    private_key=body["private_key"],
                    webhook_secret=body["webhook_secret"],
                    reason=args.reason,
                )
            elif verb == "add-repository":
                body = _add_repository_body(args)
                result = github.add_repository(
                    admin,
                    uow,
                    principal=principal,
                    installation_id=body["installation_id"],
                    repository=body["repository"],
                    name=body["name"],
                    policy_name=body["policy_name"],
                    attested_all_prs=body["attested_all_prs"],
                    attested_by=body["attested_by"],
                    reason=args.reason,
                )
            else:
                result = github.check(admin, uow, principal=principal, reason=args.reason)
            uow.commit()
            return result
    if command == "gateway":
        return _local_gateway(args, wiring, admin)
    if command == "audit":
        with wiring.ctx.uow_factory() as uow:
            return audit.tail(uow, cursor=args.cursor, limit=args.limit)
    if command == "routing":
        with wiring.ctx.uow_factory() as uow:
            if args.routing_command == "exhaustion":
                return routing.list_exhaustions(admin, uow)
            if args.routing_command == "clear-exhaustion":
                result = routing.clear_exhaustion(
                    admin, uow, principal=principal, pool=args.pool, reason=args.reason
                )
                uow.commit()
                return result
            if args.routing_command == "local-endpoint":
                return routing.local_endpoint_view(uow)
            result = routing.save_local_endpoint(
                admin,
                uow,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                endpoint_url=args.endpoint_url,
                models=[
                    {
                        "id": args.model,
                        "enabled": args.enable,
                        "enable_thinking": args.enable_thinking,
                    }
                ],
                max_concurrency=args.max_concurrency,
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "kubernetes":
        with wiring.ctx.uow_factory() as uow:
            if args.kubernetes_command == "egress":
                return kubernetes_admin.egress_view(admin, uow)
            result = kubernetes_admin.save_egress(
                admin, uow, principal=principal, document=_egress(args), reason=args.reason
            )
            uow.commit()
            return result
    if command == "bootstrap":
        return _local_bootstrap(args, wiring, admin, principal)
    raise UsageError(f"unknown command: {command}")


def _local_gateway(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
    principal = Principal(
        id=CLI_PRINCIPAL, name=CLI_PRINCIPAL, role=Role.ADMIN, created_at=wiring.ctx.clock.now()
    )
    verb = args.gateway_command
    with wiring.ctx.uow_factory() as uow:
        if verb == "show":
            return gateway.gateway_view(admin, uow)
        if verb == "models":
            return asyncio.run(gateway.models_view(admin, uow))
        if verb == "test":
            result = asyncio.run(
                gateway.test_gateway(admin, uow, principal=principal, reason=args.reason)
            )
        elif verb == "pick":
            result = asyncio.run(
                gateway.save_models(
                    admin,
                    uow,
                    principal=principal,
                    models=_picks(args),
                    max_concurrency=args.max_concurrency,
                    reason=args.reason,
                )
            )
        else:
            result = asyncio.run(
                gateway.save_gateway(
                    admin,
                    uow,
                    principal=principal,
                    endpoint_url=args.endpoint_url,
                    api_key=_read_api_key() if args.key else None,
                    reason=args.reason,
                )
            )
        uow.commit()
        return result


def _local_credentials(
    args: argparse.Namespace, wiring: Wiring, admin: AdminContext, principal: str
) -> Any:
    verb = args.credential_command
    if verb == "login":
        return _local_login(args, wiring, admin)
    with wiring.ctx.uow_factory() as uow:
        if verb == "status":
            return credentials.state_view(admin, uow, args.harness)
        if verb == "set":
            result = asyncio.run(
                credentials.set_api_key(
                    admin,
                    uow,
                    principal=principal,
                    harness=args.harness,
                    api_key=_read_api_key(),
                    reason=args.reason,
                )
            ).as_dict()
        elif verb == "validate":
            result = asyncio.run(
                credentials.validate(
                    admin, uow, principal=principal, harness=args.harness, reason=args.reason
                )
            ).as_dict()
        elif verb == "probe":
            result = asyncio.run(
                credentials.probe(
                    admin, uow, principal=principal, harness=args.harness, reason=args.reason
                )
            ).as_dict()
        elif verb == "rotate":
            result = credentials.rotate(
                admin,
                uow,
                principal=principal,
                harness=args.harness,
                new_path=args.new_path,
                reason=args.reason,
            ).as_dict()
        else:
            result = credentials.remove(
                admin, uow, principal=principal, harness=args.harness, reason=args.reason
            ).as_dict()
        uow.commit()
        return result


def _local_bootstrap(
    args: argparse.Namespace, wiring: Wiring, admin: AdminContext, principal: str
) -> Any:
    """15 through the same services the API calls. The bundle file is read here and
    handed over parsed; every rule of step 2 is the service's, on both entry points."""
    verb = args.bootstrap_command
    if verb == "submit":
        bundle = _read_bundle(args.file)
        with wiring.ctx.uow_factory() as uow:
            report, _created = bootstrap.submit(
                admin,
                uow,
                principal=principal,
                bundle=bundle,
                reason=args.reason,
                owner=args.owner,
            )
            uow.commit()
        return report
    with wiring.ctx.uow_factory() as uow:
        if verb == "show":
            return bootstrap.show(uow, args.import_id)
        if verb == "list":
            return {"items": bootstrap.list_imports(uow)}
        result = bootstrap.commit(
            admin, uow, principal=principal, import_id=args.import_id, reason=args.reason
        )
        uow.commit()
        return result


def _token(args: argparse.Namespace, wiring: Wiring) -> Any:
    if wiring.admin is None:
        raise _not_configured()
    with wiring.ctx.uow_factory() as uow:
        if args.token_command == "list":
            return {"items": tokens_admin.list_principals(uow)}
        if args.token_command == "revoke":
            result = tokens_admin.revoke(
                wiring.admin,
                uow,
                principal=CLI_PRINCIPAL,
                principal_id=args.principal_id,
                reason=args.reason,
            )
            uow.commit()
            tokens_admin.after_revoke(wiring.admin, result)
            return result
        if args.rotate:
            raise UsageError("token rotation is replaced by revoke and create")
        minted = tokens_admin.create(
            wiring.admin,
            uow,
            principal=CLI_PRINCIPAL,
            name=args.principal,
            role=args.role,
            reason=args.reason,
        )
        uow.commit()
    # The token is printed exactly once and never stored in clear.
    return {
        "principal": minted.principal.name,
        "role": minted.principal.role.value,
        "token": minted.token,
    }


def ensure_first_admin(database_url: str, delivery: FirstRunDelivery | None) -> None:
    """Create the first browser principal only when no administrator exists.

    Its token never reaches stdout, stderr or a log (crucible#122, ADR 0016): it goes to
    `delivery`, a Secret on Kubernetes or a mode 0600 file on Docker, before the
    principal is committed, so a token that could not be handed over is never minted.
    The log says only where to read it. Without a delivery nothing is minted and the log
    says how to make an administrator instead. A rerun sees the principal and does
    nothing.
    """
    engine = make_engine(database_url)
    try:
        factory = SqlUnitOfWorkFactory(engine)
        with factory() as uow:
            if any(
                item.role is Role.ADMIN and item.disabled_at is None
                for item in uow.principals.list_all()
            ):
                return
            if delivery is None:
                print(
                    "No first-run administrator was created: this deployment has no "
                    "private place for its token (the Kubernetes provider's Secret or the "
                    "Docker credential root). With the supervisor running, create one "
                    'with `crucible admin --reason "<why>" token create --principal '
                    "<name> --role admin`.",
                    file=sys.stderr,
                )
                return
            name = FIRST_RUN_PREFIX
            if uow.principals.get_by_name(name) is not None:
                name = f"{FIRST_RUN_PREFIX}-{new_id()[-8:].lower()}"
            minted = mint_token(
                uow,
                SystemClock(),
                name=name,
                role=Role.ADMIN,
            )
            record_event(
                uow,
                SystemClock(),
                EventKind.PRINCIPAL_CREATED,
                principal="crucible-migrate",
                payload={
                    "principal": minted.principal.name,
                    "role": minted.principal.role.value,
                    "first_run": True,
                },
            )
            delivery.deliver(minted.token)
            uow.commit()
        border = "=" * 72
        for line in (
            border,
            f"CRUCIBLE FIRST-RUN ADMINISTRATOR {minted.principal.name!r} CREATED",
            f"Its one-time token is in {delivery.where()}",
            "Open /ui and sign in with it; that removes it from there.",
            border,
        ):
            print(line, file=sys.stderr)
    finally:
        engine.dispose()


def _register(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
    """The same guarded service the API route calls, returning the same document."""
    with wiring.ctx.uow_factory() as uow:
        if args.repo_command == "list":
            return {"items": repositories_admin.list_all(uow)}
        if args.repo_command == "remove":
            result = repositories_admin.remove(
                admin, uow, principal=CLI_PRINCIPAL, name=args.name, reason=args.reason
            )
            uow.commit()
            return result
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
    return result


# ----- the envelope ------------------------------------------------------------


def kind_of(args: argparse.Namespace) -> str:
    """The `kind` each verb's document is (crucible/client/schema.py)."""
    command = str(args.command)
    verb = {
        "task": "task_command",
        "token": "token_command",
        "repository": "repo_command",
        "repositories": "repo_command",
        "harnesses": "harness_command",
        "credentials": "credential_command",
        "images": "image_command",
        "providers": "provider_command",
        "github": "github_command",
        "audit": "audit_command",
        "routing": "routing_command",
        "kubernetes": "kubernetes_command",
        "bootstrap": "bootstrap_command",
        "gateway": "gateway_command",
    }.get(command)
    sub = getattr(args, verb) if verb else None
    table: dict[tuple[str, str | None], str] = {
        ("migrate", None): "migration",
        ("status", None): "admin_status",
        ("task", "republish"): "task",
        ("token", "list"): "token_list",
        ("token", "create"): "token_created",
        ("token", "revoke"): "token_revoked",
        ("repository", "list"): "repository_list",
        ("repository", "register"): "repository",
        ("repository", "remove"): "repository_removed",
        ("harnesses", "list"): "harness_list",
        ("harnesses", "enable"): "harness",
        ("harnesses", "disable"): "harness",
        ("credentials", "status"): "credential_state",
        ("credentials", "login"): "credential_login",
        ("images", "list"): "image_list",
        ("images", "promote"): "image_promotion",
        ("providers", "status"): "provider_list",
        ("github", "status"): "github_status",
        ("github", "check"): "github_check",
        ("github", "connect"): "github_connected",
        ("github", "installations"): "github_installations",
        ("github", "add-repository"): "repository",
        ("gateway", "show"): "gateway",
        ("gateway", "set"): "gateway_test",
        ("gateway", "test"): "gateway_test",
        ("gateway", "models"): "gateway_models",
        ("gateway", "pick"): "gateway_models_saved",
        ("audit", "tail"): "audit_page",
        ("routing", "exhaustion"): "exhaustion_list",
        ("routing", "clear-exhaustion"): "exhaustion_cleared",
        ("routing", "local-endpoint"): "local_endpoint",
        ("routing", "set-local-endpoint"): "local_endpoint",
        ("kubernetes", "egress"): "kubernetes_egress",
        ("kubernetes", "set-egress"): "kubernetes_egress",
        ("bootstrap", "list"): "bootstrap_import_list",
    }
    key = ("repository" if command == "repositories" else command, sub)
    if key in table:
        return table[key]
    if command == "credentials":
        return "credential_report"
    if command == "bootstrap":
        return "bootstrap_import"
    return command


def _items(document: Any) -> list[Any]:
    items = document.get("items") if isinstance(document, dict) else None
    return items if isinstance(items, list) else []


def result_for(
    args: argparse.Namespace,
    document: Any,
    *,
    prefix: list[str],
    top_prefix: list[str],
    local: bool,
    role: str,
) -> Result:
    kind = kind_of(args)
    state: str | None = None
    actions: list[dict[str, Any]] = []
    if kind == "task":
        state = document.get("state") if isinstance(document, dict) else None
        if local:
            # In process the principal is this CLI's admin; its one task verb is republish.
            if state == "publish_failed":
                actions = [
                    nx.action(
                        "republish",
                        "retry the failed publication once",
                        [*prefix, "task", "republish", str(document["id"]), "--reason", "{reason}"],
                        needs=nx.REASON,
                        roles=(nx.ADMIN,),
                    )
                ]
        else:
            actions = nx.task_actions(document, role, top_prefix)
    elif kind in ("credential_state", "credential_report"):
        credential = document.get("credential", document) if isinstance(document, dict) else {}
        state = credential.get("state") if isinstance(credential, dict) else None
        actions = nx.credential_actions(args.harness, state, prefix)
    elif kind == "harness_list":
        actions = nx.harness_actions(_items(document), prefix)
    elif kind == "harness" and isinstance(document, dict):
        state = "enabled" if document.get("enabled") else "disabled"
        actions = nx.harness_actions(
            [{"name": args.name, "enabled_by_administrator": args.harness_command == "enable"}],
            prefix,
        )
    elif kind == "image_list":
        actions = nx.image_actions(_items(document), prefix)
    elif kind == "exhaustion_list":
        actions = nx.exhaustion_actions(_items(document), prefix)
    elif kind == "token_list":
        actions = nx.token_actions(_items(document), prefix)
    elif kind == "bootstrap_import" and isinstance(document, dict):
        state = document.get("state")
        actions = nx.bootstrap_actions(document, prefix)
    elif kind == "local_endpoint":
        actions = nx.local_endpoint_actions(document, prefix)
    elif kind == "kubernetes_egress":
        actions = nx.kubernetes_egress_actions(document, prefix)
    elif kind == "audit_page":
        actions = nx.audit_actions(document, prefix, args.limit, args.cursor)
    return Result(kind=kind, data=document, state=state, next=actions, role=role)


def run(args: argparse.Namespace, *, root_api_url: str | None, timezone: str | None) -> Result:
    """One admin verb, local or remote, as a Result for the envelope."""
    api_url = args.api_url or root_api_url
    if api_url or args.remote:
        if args.command == "migrate":
            raise UsageError("migrate is CLI-only and runs in local mode; drop --api-url")
        config = resolve(api_url=api_url, timezone=timezone, token_envs=TOKEN_ENVS)
        base_url, token = require_remote(config, TOKEN_ENVS)
        remote_document = _remote(args, Api(base_url, token))
        flag = ["--api-url", base_url] if api_url else ["--remote"]
        top = ["crucible", *(["--api-url", base_url] if api_url else [])]
        # Every admin route admits only an admin; `task republish` is the orchestrator's.
        role = nx.PROBED_ORCHESTRATOR if args.command == "task" else nx.ADMIN
        return result_for(
            args,
            remote_document,
            prefix=["crucible", "admin", *flag],
            top_prefix=top,
            local=False,
            role=role,
        )
    settings = load_settings(args.config)
    # Results go to stdout as JSON; logs go to stderr so callers can parse stdout.
    configure_logging(settings.service.log_level, stream=sys.stderr)
    prefix = ["crucible", "admin", *(["--config", args.config] if args.config else [])]
    try:
        if args.command == "migrate":
            upgrade(settings.database.url)
            ensure_first_admin(settings.database.url, first_run_delivery(settings))
            document: Any = {"migrated_to": head_revision(settings.database.url)}
        else:
            wiring = wire(settings)
            if args.command == "token":
                document = _token(args, wiring)
            elif args.command in ("repository", "repositories"):
                if wiring.admin is None:
                    raise _not_configured()
                document = _register(args, wiring, wiring.admin)
            else:
                document = _local(args, wiring)
    except ApplicationError as exc:
        raise application_error(exc) from None
    return result_for(
        args, document, prefix=prefix, top_prefix=["crucible"], local=True, role=nx.ADMIN
    )
