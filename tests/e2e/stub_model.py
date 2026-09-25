"""A scripted model server for the command-timeout tier (issue 128).

It runs inside the worker container, on loopback, under `--network none`, so a real
harness talks to it and to nothing else: no login, no subscription, no model. It speaks
the three wire shapes the image's harnesses use (the Anthropic Messages API for Claude
Code, the OpenAI Responses API for Codex, OpenAI chat completions for Hermes) and plays
one script: the first turn that offers a shell tool asks for `STUB_COMMAND`; once any
tool result is in the conversation, it ends the turn with a short text. Every request
body is appended to `STUB_LOG` so the test can read what the harness sent back.

Standard library only: the worker image has python3 and nothing else is installed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

COMMAND = os.environ.get("STUB_COMMAND", "true")
LOG = os.environ.get("STUB_LOG", "/tmp/stub-model.jsonl")
MODEL = "stub-model"
# Codex's unified exec answers a long command with a session to poll (issue 128).
RUNNING = re.compile(r"Process running with session ID (\d+)")
SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "terminal", "shell_command")


def _log(entry: dict[str, Any]) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _tool_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name") or tool.get("type")
        if isinstance(name, str):
            names.append(name)
    return names


def _has_tool_result(body: dict[str, Any]) -> bool:
    text = json.dumps(body.get("messages") or body.get("input") or [])
    return any(
        marker in text for marker in ('"tool_result"', '"function_call_output"', '"role": "tool"')
    )


def _running_session(body: dict[str, Any]) -> int | None:
    """The session a Codex exec call left running, when the last output names one and
    polling is enabled for the run (`STUB_POLL=1`, the well-behaved model)."""
    if os.environ.get("STUB_POLL") != "1":
        return None
    items = body.get("input") or []
    last = items[-1] if items and isinstance(items[-1], dict) else {}
    if last.get("type") != "function_call_output":
        return None
    match = RUNNING.search(json.dumps(last.get("output")))
    return int(match.group(1)) if match else None


def _shell_tool(body: dict[str, Any]) -> str | None:
    names = _tool_names(body)
    for wanted in SHELL_TOOLS:
        if wanted in names:
            return wanted
    return None


def _arguments(tool: str) -> dict[str, Any]:
    if tool == "Bash":
        return {"command": COMMAND, "description": "the scripted command"}
    if tool in ("shell", "local_shell"):
        return {"command": ["bash", "-lc", COMMAND]}
    if tool == "exec_command":
        return {"cmd": COMMAND}
    if tool == "shell_command":
        return {"command": COMMAND}
    if os.environ.get("STUB_BACKGROUND") == "1":
        # Hermes's own background mode, as a model that chooses it would ask for it.
        return {"command": COMMAND, "background": True, "notify_on_complete": True}
    return {"command": COMMAND}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _json(self, status: int, document: Any) -> None:
        data = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events: list[tuple[str | None, dict[str, Any]]], *, done: bool = False) -> None:
        chunks = []
        for name, data in events:
            prefix = f"event: {name}\n" if name else ""
            chunks.append(f"{prefix}data: {json.dumps(data)}\n\n")
        if done:
            chunks.append("data: [DONE]\n\n")
        payload = "".join(chunks).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            return
        self._json(200, {})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        body = self._body()
        path = self.path.split("?", 1)[0]
        tool = _shell_tool(body)
        call = tool is not None and not _has_tool_result(body)
        _log({"at": time.time(), "path": path, "call": call, "tool": tool, "body": body})
        if path.endswith("/messages/count_tokens"):
            self._json(200, {"input_tokens": 1})
        elif path.endswith("/messages"):
            self._anthropic(body, tool if call else None)
        elif path.endswith("/responses"):
            session = _running_session(body)
            if session is not None:
                self._responses("write_stdin", {"session_id": session, "yield_time_ms": 300000})
            else:
                self._responses(tool if call else None)
        elif path.endswith("/chat/completions"):
            self._chat(body, tool if call else None)
        else:
            self._json(404, {"error": {"message": f"no stub for {self.path}"}})

    # ----- Anthropic Messages (Claude Code) ------------------------------------

    def _anthropic(self, body: dict[str, Any], tool: str | None) -> None:
        usage = {"input_tokens": 1, "output_tokens": 1}
        if tool is not None:
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": "toolu_stub_1",
                "name": tool,
                "input": _arguments(tool),
            }
            stop = "tool_use"
        else:
            block = {"type": "text", "text": "The scripted command has returned."}
            stop = "end_turn"
        message = {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", MODEL),
            "content": [block],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            self._json(200, message)
            return
        start_block = dict(block, input={}) if tool else dict(block, text="")
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            if tool
            else {"type": "text_delta", "text": block["text"]}
        )
        self._sse(
            [
                ("message_start", {"type": "message_start", "message": dict(message, content=[])}),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": start_block},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 1},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )

    # ----- OpenAI Responses (Codex) --------------------------------------------

    def _responses(self, tool: str | None, arguments: dict[str, Any] | None = None) -> None:
        if tool is not None:
            item: dict[str, Any] = {
                "type": "function_call",
                "id": f"fc_stub_{int(time.time() * 1000)}",
                "call_id": f"call_stub_{int(time.time() * 1000)}",
                "name": tool,
                "arguments": json.dumps(arguments or _arguments(tool)),
                "status": "completed",
            }
        else:
            item = {
                "type": "message",
                "id": "msg_stub",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The scripted command has returned.",
                        "annotations": [],
                    }
                ],
            }
        response = {"id": "resp_stub", "object": "response", "model": MODEL, "output": []}
        usage = {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        }
        self._sse(
            [
                ("response.created", {"type": "response.created", "response": response}),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": dict(response, output=[item], usage=usage, status="completed"),
                    },
                ),
            ]
        )

    # ----- OpenAI chat completions (Hermes) ------------------------------------

    def _chat(self, body: dict[str, Any], tool: str | None) -> None:
        if tool is not None:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stub_1",
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(_arguments(tool))},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "The scripted command has returned."}
            finish = "stop"
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not body.get("stream"):
            self._json(
                200,
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model", MODEL),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )
            return
        delta = dict(message)
        if tool is not None:
            delta["tool_calls"] = [dict(message["tool_calls"][0], index=0)]
        base = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model", MODEL),
        }
        self._sse(
            [
                (None, dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}])),
                (
                    None,
                    dict(
                        base,
                        choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                        usage=usage,
                    ),
                ),
            ],
            done=True,
        )


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
