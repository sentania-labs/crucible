"""A local fake of the Crucible HTTP API for the client tests. Nothing here can reach a
real Crucible: it listens on 127.0.0.1 on a free port and answers what the test set."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

TOKEN = "cru_" + "t" * 26 + "." + "k" * 40


class FakeCrucible:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.response: Any = {"schema_version": "1.0", "status": "ok"}
        self.responses: list[Any] = []
        # path (without query) -> (status, body): the role probes and other fixed routes.
        self.routes: dict[str, tuple[int, Any]] = {}
        self.response_headers: dict[str, str] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def role(self, role: str) -> None:
        """Answer the two role probes as the API's guards would for `role`."""
        admin = 200 if role == "admin" else 403
        orchestrator = 200 if role in ("orchestrator", "operator") else 403
        self.routes["/v1/admin/audit"] = (admin, {"items": [], "next_cursor": 0})
        self.routes["/v1/capabilities"] = (orchestrator, {"harnesses": []})

    def calls(self) -> list[tuple[str, str, Any]]:
        """The requests the command itself made, without the role probes."""
        probes = ("/v1/admin/audit?limit=1", "/v1/capabilities")
        return [
            (r["method"], r["path"], r["body"]) for r in self.requests if r["path"] not in probes
        ]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(raw) if raw else None,
                    }
                )
                route = owner.routes.get(self.path.split("?", 1)[0])
                if route is not None:
                    status, response = route
                else:
                    status = owner.status
                    response = owner.responses.pop(0) if owner.responses else owner.response
                body = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in owner.response_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        return Handler


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[FakeCrucible]:
    server = FakeCrucible()
    monkeypatch.setenv("CRUCIBLE_URL", server.url)
    monkeypatch.setenv("CRUCIBLE_TOKEN", TOKEN)
    monkeypatch.delenv("CRUCIBLE_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("CRUCIBLE_CLIENT_CONFIG", str(tmp_path / "absent-client.toml"))
    try:
        yield server
    finally:
        server.close()
