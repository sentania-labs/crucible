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
installation's repositories.

Configuration is one JSON document, from `--config FILE` or the `FIRST_RUN_STUBS`
environment variable:

    {"gateway_key": "...", "models": ["a", "b"],
     "app_id": 4242, "app_slug": "crucible-kind", "public_key_pem": "-----BEGIN ...",
     "installations": [{"id": 7, "account": "octo-lab", "type": "Organization",
                        "repositories": [{"full_name": "octo-lab/widgets",
                                          "default_branch": "trunk"}]}]}

Stdlib plus `cryptography`, which the service image already carries.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Stubs:
    """The state both halves answer from, and what they saw (never a key)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.tokens: dict[str, int] = {}
        self.seen: list[str] = []
        self.lock = threading.Lock()
        pem = str(config.get("public_key_pem") or "")
        self.public_key = serialization.load_pem_public_key(pem.encode()) if pem else None

    def jwt_ok(self, token: str) -> bool:
        """RS256 over the first two parts, with `iss` the configured App id and a live
        `exp`, the checks GitHub makes."""
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
        if length:
            self.rfile.read(length)
        self._route("POST")

    def _route(self, method: str) -> None:
        stubs = self.server.stubs
        path = urlsplit(self.path).path.rstrip("/")
        with stubs.lock:
            stubs.seen.append(f"{method} {path}")
        config = stubs.config
        if method == "GET" and path == "/health/readiness":
            self._send(200, {"status": "healthy"})
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
            installation_id = stubs.tokens.get(self._bearer())
            found = stubs.installation(installation_id) if installation_id else None
            if found is None:
                self._send(401, {"message": "Bad credentials"})
                return
            repositories = [
                {
                    "full_name": repo["full_name"],
                    "owner": {"login": repo["full_name"].split("/", 1)[0]},
                    "html_url": f"https://github.com/{repo['full_name']}",
                    "clone_url": f"https://github.com/{repo['full_name']}.git",
                    "default_branch": repo.get("default_branch", "main"),
                    "private": repo.get("private", True),
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
            token = "ghs_" + secrets.token_urlsafe(30)
            with stubs.lock:
                stubs.tokens[token] = installation_id
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            self._send(201, {"token": token, "expires_at": expires, "permissions": {}})
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=None, help="the JSON configuration file")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.config:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
    else:
        config = json.loads(os.environ["FIRST_RUN_STUBS"])
    server = _Server(("0.0.0.0", args.port), _Handler)
    server.stubs = Stubs(config)
    print(f"first-run stubs listening on :{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
