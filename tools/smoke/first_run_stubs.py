#!/usr/bin/env python3
"""Stand-ins for the two outside services the first-run setup talks to (crucible#119,
#120, #121): an OpenAI-compatible gateway and the GitHub App API.

The integration tier runs it on loopback and the kind proof runs it as a Pod, so both
exercise the service's real HTTP code. It never reaches the internet and never calls the
real GitHub API.

The gateway answers `/health/readiness` and, for the one key it was given, `/v1/models`.
The GitHub half verifies each App JWT against the public key it was given, RS256 and
`iss` equal to the App id, so a key that does not belong to the App is refused exactly
as GitHub refuses it (HTTP 401). An installation token it mints lists only that
installation's repositories. A mint that names repositories the installation does not
cover is refused (HTTP 422), as GitHub refuses it; each mint's requested repositories
and permissions are recorded, and `DELETE /installation/token` revokes a token (ADR 0019).

`--git-root` runs the third stand-in instead (crucible#157): a git remote over HTTPS that
serves each repository the configuration gives a `git` block, and answers a clone only
with Basic credentials `x-access-token:<token>` for a token the GitHub half minted,
did not revoke, and scoped to that repository with `contents: read`. It asks the GitHub
half (`--auth-url`) whether a token qualifies, so the two may run in separate
containers; this mode needs git and nothing outside the standard library.

Configuration is one JSON document, from `--config FILE` or the `FIRST_RUN_STUBS`
environment variable:

    {"gateway_key": "...", "models": ["a", "b"],
     "app_id": 4242, "app_slug": "crucible-kind", "public_key_pem": "-----BEGIN ...",
     "installations": [{"id": 7, "account": "octo-lab", "type": "Organization",
                        "repositories": [{"full_name": "octo-lab/widgets",
                                          "default_branch": "trunk"}]}]}

A repository may also carry `"url"` (the `html_url` the picker registers, in place of
`https://github.com/<full_name>`) and `"git": {"files": {"README.md": "..."}}` (what the
git stand-in commits to its default branch).

Stdlib plus `cryptography`, which the service image already carries.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Stubs:
    """The state both halves answer from, and what they saw (never a key)."""

    def __init__(self, config: dict[str, Any]) -> None:
        # cryptography is imported here and not at the top, so the git stand-in can run
        # from an image that carries git and Python but not cryptography.
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        self.config = config
        self.tokens: dict[str, dict[str, Any]] = {}
        # What each mint asked for: the installation, the repositories, the permissions.
        # Never the token.
        self.mints: list[dict[str, Any]] = []
        self.revoked: int = 0
        # Every token this stand-in minted, revoked or not, so a proof can look for one
        # where it must not be (`GET /_stub/minted`). These are the stand-in's own values
        # and grant nothing outside it.
        self.history: list[dict[str, Any]] = []
        self.seen: list[str] = []
        self.lock = threading.Lock()
        pem = str(config.get("public_key_pem") or "")
        self.public_key = serialization.load_pem_public_key(pem.encode()) if pem else None

    def jwt_ok(self, token: str) -> bool:
        """RS256 over the first two parts, with `iss` the configured App id and a live
        `exp`, the checks GitHub makes."""
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: PLC0415

        if self.public_key is None or not isinstance(self.public_key, rsa.RSAPublicKey):
            return False
        try:
            header, payload, signature = token.split(".")
            claims = json.loads(_b64decode(payload))
            self.public_key.verify(
                _b64decode(signature),
                f"{header}.{payload}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (ValueError, InvalidSignature):
            return False
        return str(claims.get("iss")) == str(self.config.get("app_id")) and int(
            claims.get("exp", 0)
        ) > int(time.time())

    def installation(self, installation_id: int) -> dict[str, Any] | None:
        for item in self.config.get("installations") or []:
            if int(item["id"]) == installation_id:
                return dict(item)
        return None

    def covered(self, installation_id: int, names: list[str]) -> list[str] | None:
        """The full names `names` resolve to in the installation (GitHub accepts the
        short name), or None when any of them is not a repository it covers."""
        found = self.installation(installation_id) or {}
        full = [str(r["full_name"]) for r in found.get("repositories") or []]
        out: list[str] = []
        for name in names:
            match = [f for f in full if f == name or f.rsplit("/", 1)[-1] == name]
            if not match:
                return None
            out.append(match[0])
        return out

    def git_access(self, token: str, repository: str) -> bool:
        """Whether `token` may clone `repository`: minted here, not revoked, and scoped
        to that repository with `contents: read` (or broader)."""
        with self.lock:
            record = self.tokens.get(token)
        if record is None:
            return False
        repositories = record.get("repositories")
        if repositories is not None and repository not in repositories:
            return False
        return record.get("permissions", {}).get("contents") in ("read", "write")


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[len("Bearer ") :] if value.startswith("Bearer ") else ""

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length) if length else b""
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        stubs = self.server.stubs
        split = urlsplit(self.path)
        path = split.path.rstrip("/")
        with stubs.lock:
            stubs.seen.append(f"{method} {path}")
        config = stubs.config
        if method == "GET" and path == "/health/readiness":
            self._send(200, {"status": "healthy"})
            return
        if method == "DELETE" and path == "/installation/token":
            with stubs.lock:
                gone = stubs.tokens.pop(self._bearer(), None)
                if gone is not None:
                    stubs.revoked += 1
                    for entry in stubs.history:
                        if entry["token"] == self._bearer():
                            entry["revoked"] = True
            if gone is None:
                self._send(401, {"message": "Bad credentials"})
                return
            self.send_response(204)
            self.end_headers()
            return
        if method == "GET" and path == "/_stub/minted":
            with stubs.lock:
                self._send(200, list(stubs.history))
            return
        if method == "GET" and path == "/_stub/git-access":
            repository = (parse_qs(split.query).get("repository") or [""])[0]
            allowed = stubs.git_access(self._bearer(), repository)
            self.send_response(204 if allowed else 403)
            self.end_headers()
            return
        if method == "GET" and path == "/v1/models":
            if self._bearer() != config.get("gateway_key"):
                self._send(401, {"error": {"message": "invalid key"}})
                return
            models = [{"id": m, "object": "model"} for m in config.get("models") or []]
            self._send(200, {"object": "list", "data": models})
            return
        if path == "/app" or path.startswith("/app/"):
            if not stubs.jwt_ok(self._bearer()):
                self._send(401, {"message": "A JSON web token could not be decoded"})
                return
            self._app(method, path)
            return
        if method == "GET" and path == "/installation/repositories":
            record = stubs.tokens.get(self._bearer())
            installation_id = int(record["installation"]) if record else None
            found = stubs.installation(installation_id) if installation_id else None
            if found is None:
                self._send(401, {"message": "Bad credentials"})
                return
            repositories = [
                {
                    "full_name": repo["full_name"],
                    "owner": {"login": repo["full_name"].split("/", 1)[0]},
                    "html_url": repo.get("url") or f"https://github.com/{repo['full_name']}",
                    "clone_url": (repo.get("url") or f"https://github.com/{repo['full_name']}")
                    + ".git",
                    "default_branch": repo.get("default_branch", "main"),
                    "private": repo.get("private", False),
                    "archived": repo.get("archived", False),
                }
                for repo in found.get("repositories") or []
            ]
            self._send(200, {"total_count": len(repositories), "repositories": repositories})
            return
        self._send(404, {"message": "Not Found"})

    def _app(self, method: str, path: str) -> None:
        stubs = self.server.stubs
        config = stubs.config
        slug = config.get("app_slug", "crucible-stub")
        if method == "GET" and path == "/app":
            self._send(
                200,
                {
                    "id": config.get("app_id"),
                    "slug": slug,
                    "name": slug,
                    "owner": {"login": "stub-owner"},
                    "html_url": f"https://github.com/apps/{slug}",
                },
            )
            return
        if method == "GET" and path == "/app/installations":
            self._send(
                200,
                [
                    {
                        "id": item["id"],
                        "account": {"login": item["account"], "type": item.get("type", "User")},
                        "repository_selection": "selected",
                        "html_url": f"https://github.com/settings/installations/{item['id']}",
                    }
                    for item in config.get("installations") or []
                ],
            )
            return
        parts = path.split("/")
        if method == "POST" and len(parts) == 5 and parts[4] == "access_tokens":
            installation_id = int(parts[3])
            if stubs.installation(installation_id) is None:
                self._send(404, {"message": "Not Found"})
                return
            try:
                request = json.loads(getattr(self, "body", b"") or b"{}")
            except ValueError:
                request = {}
            names = request.get("repositories")
            permissions = {str(k): str(v) for k, v in (request.get("permissions") or {}).items()}
            repositories = None
            if names is not None:
                repositories = stubs.covered(installation_id, [str(n) for n in names])
                if repositories is None:
                    self._send(
                        422,
                        {
                            "message": "There is at least one repository that does not exist "
                            "or is not accessible to the parent installation."
                        },
                    )
                    return
            granted = {**permissions, "metadata": "read"} if permissions else {}
            token = "ghs_" + secrets.token_urlsafe(30)
            with stubs.lock:
                stubs.tokens[token] = {
                    "installation": installation_id,
                    "repositories": repositories,
                    # No `permissions` field is the App's whole grant, which the
                    # first-run App holds as contents write.
                    "permissions": permissions or {"contents": "write", "metadata": "read"},
                }
                stubs.mints.append(
                    {
                        "installation": installation_id,
                        "repositories": names,
                        "permissions": permissions,
                    }
                )
                stubs.history.append(
                    {
                        "token": token,
                        "repositories": names,
                        "permissions": permissions,
                        "revoked": False,
                    }
                )
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            self._send(201, {"token": token, "expires_at": expires, "permissions": granted})
            return
        self._send(404, {"message": "Not Found"})


class _Server(ThreadingHTTPServer):
    stubs: Stubs


class StubServer:
    """The stubs on a loopback port, for a test: `with StubServer(config) as s: s.url`."""

    def __init__(self, config: dict[str, Any], *, host: str = "127.0.0.1", port: int = 0):
        self._server = _Server((host, port), _Handler)
        self._server.stubs = Stubs(config)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def stubs(self) -> Stubs:
        return self._server.stubs

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


# ----- the git stand-in (crucible#157) ----------------------------------------------


def _git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> bytes:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, env=env, capture_output=True, check=True
    ).stdout


def seed_repositories(config: dict[str, Any], root: Path) -> list[str]:
    """A bare repository per configured repository with a `git` block, holding one
    commit of its files on its default branch. Returns the full names served."""
    served: list[str] = []
    for installation in config.get("installations") or []:
        for repo in installation.get("repositories") or []:
            if "git" not in repo:
                continue
            full_name = str(repo["full_name"])
            branch = str(repo.get("default_branch") or "main")
            bare = root / f"{full_name}.git"
            if not bare.exists():
                with tempfile.TemporaryDirectory() as work:
                    tree = Path(work)
                    _git("init", "-q", "-b", branch, cwd=tree)
                    for name, text in (repo["git"].get("files") or {"README.md": "stub\n"}).items():
                        target = tree / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(str(text), encoding="utf-8")
                    _git("add", "-A", cwd=tree)
                    _git(
                        "-c",
                        "user.name=stub",
                        "-c",
                        "user.email=stub@example.invalid",
                        "commit",
                        "-q",
                        "-m",
                        "seed",
                        cwd=tree,
                    )
                    bare.parent.mkdir(parents=True, exist_ok=True)
                    _git("clone", "-q", "--bare", str(tree), str(bare))
            served.append(full_name)
    return served


def _pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


class _GitHandler(BaseHTTPRequestHandler):
    """git's smart HTTP protocol, read-only (`git-upload-pack`), for the served
    repositories, behind the check a GitHub remote makes of an installation token."""

    server: _GitServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _deny(self, status: int, why: str) -> None:
        self.server.note(f"{status} {self.command} {urlsplit(self.path).path}: {why}")
        body = why.encode() + b"\n"
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="crucible git stand-in"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _repository(self) -> tuple[str, Path] | None:
        path = urlsplit(self.path).path
        for suffix in ("/info/refs", "/git-upload-pack"):
            if path.endswith(suffix):
                name = path[: -len(suffix)].strip("/").removesuffix(".git")
                bare = self.server.root / f"{name}.git"
                if name in self.server.served and bare.is_dir():
                    return name, bare
        return None

    def _token(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Basic "):
            return ""
        try:
            user, _, password = base64.b64decode(value[6:]).decode().partition(":")
        except ValueError:
            return ""
        return password if user == "x-access-token" else ""

    def _authorized(self, repository: str) -> bool:
        token = self._token()
        if not token:
            self._deny(401, "a token is required")
            return False
        request = urllib.request.Request(
            f"{self.server.auth_url}/_stub/git-access?{urlencode({'repository': repository})}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            # The GitHub half is local to this stand-in: never through a proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                allowed = bool(response.status == 204)
        except urllib.error.HTTPError:
            allowed = False
        if not allowed:
            self._deny(403, "the token may not read this repository")
        return allowed

    def _reply(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        found = self._repository()
        query = parse_qs(urlsplit(self.path).query)
        if found is None or query.get("service") != ["git-upload-pack"]:
            self._deny(404, "not found")
            return
        name, bare = found
        if not self._authorized(name):
            return
        refs = _git("upload-pack", "--stateless-rpc", "--advertise-refs", str(bare))
        self.server.note(f"200 GET info/refs for {name} with a scoped token")
        self._reply(
            "application/x-git-upload-pack-advertisement",
            _pkt("# service=git-upload-pack\n") + b"0000" + refs,
        )

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts: list[bytes] = []
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline()
            data = b"".join(parts)
        else:
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
        return data

    def do_POST(self) -> None:
        found = self._repository()
        body = self._body()
        if found is None or not urlsplit(self.path).path.endswith("/git-upload-pack"):
            self._deny(404, "not found")
            return
        name, bare = found
        if not self._authorized(name):
            return
        pack = _git("upload-pack", "--stateless-rpc", str(bare), stdin=body)
        self.server.note(f"200 POST git-upload-pack for {name} with a scoped token")
        self._reply("application/x-git-upload-pack-result", pack)


class _GitServer(ThreadingHTTPServer):
    root: Path
    served: list[str]
    auth_url: str
    notes: list[str]

    def note(self, line: str) -> None:
        self.notes.append(line)
        print(f"git stand-in: {line}", flush=True)


class GitStubServer:
    """The git stand-in over HTTPS on a loopback port, for a test."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        root: Path,
        auth_url: str,
        cert: str,
        key: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._server = _git_server(config, root, auth_url, cert, key, host, port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def notes(self) -> list[str]:
        return self._server.notes

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> GitStubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def _git_server(
    config: dict[str, Any], root: Path, auth_url: str, cert: str, key: str, host: str, port: int
) -> _GitServer:
    server = _GitServer((host, port), _GitHandler)
    server.root = root
    server.served = seed_repositories(config, root)
    server.auth_url = auth_url.rstrip("/")
    server.notes = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=None, help="the JSON configuration file")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--git-root", default=None, help="serve git from here instead")
    parser.add_argument("--auth-url", default="http://127.0.0.1:8080", help="the GitHub half")
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args()
    if args.config:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
    else:
        config = json.loads(os.environ["FIRST_RUN_STUBS"])
    if args.git_root:
        if not args.tls_cert or not args.tls_key:
            parser.error("--git-root needs --tls-cert and --tls-key")
        git = _git_server(
            config,
            Path(args.git_root),
            args.auth_url,
            args.tls_cert,
            args.tls_key,
            "0.0.0.0",
            args.port,
        )
        print(f"git stand-in serving {git.served} on :{args.port}", flush=True)
        git.serve_forever()
        return 0
    server = _Server(("0.0.0.0", args.port), _Handler)
    server.stubs = Stubs(config)
    print(f"first-run stubs listening on :{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
