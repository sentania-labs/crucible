"""The orchestrator verbs (04): what each sends, and what it returns as an envelope.

Carried from Foundry's `foundry-crucible`, verb for verb: the same paths, bodies, the
`X-Foundry-Reason` header, pagination, and the versioned-response check. What is new is
the envelope around the result and the `next` actions for the principal in use.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crucible.client import next as nx
from crucible.client.envelope import ClientError, Result, UsageError
from crucible.client.http import ORCHESTRATOR_TIMEOUT_SECONDS, Api
from crucible.client.roles import probe_role


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: Any = None


# The role class each verb's route admits (crucible/adapters/api/deps.py). A verb whose
# route admits only the orchestrator class proves the role by succeeding.
ORCHESTRATOR_ONLY = frozenset(
    {"accept", "review", "dispositions", "ci-decision", "head-decision", "close", "republish"}
)
TASK_RESULT = frozenset(
    {
        "submit",
        "start",
        "accept",
        "review",
        "dispositions",
        "corrections",
        "ci-decision",
        "head-decision",
        "decisions",
        "cancel",
        "close",
        "republish",
    }
)
VERBS = ("tasks", "task", "wakes", *sorted(TASK_RESULT), "health")
RELATED = ("events", "pull_request", "attempts", "gates", "report", "evidence")


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read JSON from {path}: {exc}") from None


def _without_none(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if value is not None}


def call_for(args: argparse.Namespace) -> Call:
    command: str = args.command
    if command == "tasks":
        query = urllib.parse.urlencode(_without_none({"state": args.state}))
        return Call("GET", "/tasks" + (f"?{query}" if query else ""))
    if command == "task":
        return Call("GET", f"/tasks/{args.id}")
    if command == "wakes":
        if args.wakes_command == "ack":
            return Call("POST", f"/wakes/{args.id}/ack", {"note": args.reason})
        return Call("GET", "/wakes")
    if command == "submit":
        return Call("POST", "/tasks", read_json(args.file))
    if command == "start":
        return Call(
            "POST",
            f"/tasks/{args.id}/start",
            _without_none(
                {
                    "harness": args.harness,
                    "model": args.model,
                    "provider": args.provider,
                    "image": args.image,
                    "policy_version": args.policy_version,
                    "effort": args.effort,
                }
            ),
        )
    if command == "accept":
        return Call(
            "POST",
            f"/tasks/{args.id}/accept",
            _without_none(
                {"verdict": args.verdict, "reasoning": args.reason, "head_sha": args.head_sha}
            ),
        )
    if command in ("review", "dispositions", "corrections"):
        return Call("POST", f"/tasks/{args.id}/{command}", read_json(args.file))
    if command == "ci-decision":
        return Call(
            "POST",
            f"/tasks/{args.id}/ci-decision",
            {"cause": args.cause, "action": args.action, "reasoning": args.reason},
        )
    if command == "head-decision":
        return Call(
            "POST",
            f"/tasks/{args.id}/head-decision",
            {"action": args.action, "reasoning": args.reason},
        )
    if command == "decisions":
        return Call(
            "POST",
            f"/tasks/{args.id}/decisions",
            _without_none(
                {
                    "kind": args.kind,
                    "verbatim": args.verbatim,
                    "resolves": args.resolves,
                    "escalation_id": args.escalation_id,
                    "reschedule": args.reschedule,
                }
            ),
        )
    if command == "cancel":
        return Call(
            "POST",
            f"/tasks/{args.id}/cancel",
            {"reason": args.reason, "verbatim": args.verbatim, "decided_by": args.decided_by},
        )
    if command == "close":
        return Call("POST", f"/tasks/{args.id}/close", {"note": args.reason})
    if command == "republish":
        return Call("POST", f"/tasks/{args.id}/republish", {"reason": args.reason})
    if command == "health":
        return Call("GET", "/health")
    raise UsageError(f"unknown command: {command}")


def _request(api: Api, call: Call, *, reason: str | None = None) -> Any:
    return api.call(
        call.method,
        "/v1" + call.path,
        call.body,
        reason=reason,
        versioned=True,
        timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    )


def _related(api: Api, args: argparse.Namespace, task: Any) -> Any:
    if not isinstance(task, dict) or not any(getattr(args, flag) for flag in RELATED):
        return task
    result: dict[str, Any] = {"task": task}
    if args.events:
        result["events"] = api.all_pages(f"/v1/tasks/{args.id}/events")
    if args.pull_request:
        result["pull_request"] = _request(api, Call("GET", f"/tasks/{args.id}/pull-request"))
    if args.attempts:
        result["attempts"] = [
            _request(api, Call("GET", f"/attempts/{attempt['id']}"))
            for execution in task.get("executions", [])
            for attempt in execution.get("attempts", [])
        ]
    latest = task.get("latest_attempt") or {}
    attempt_id = latest.get("id")
    for flag in ("gates", "report", "evidence"):
        if getattr(args, flag):
            if not attempt_id:
                raise UsageError(f"task {args.id} has no latest attempt for --{flag}")
            result[flag] = _request(api, Call("GET", f"/attempts/{attempt_id}/{flag}"))
    return result


def run(api: Api, args: argparse.Namespace, prefix: Sequence[str]) -> Result:
    """One verb against the API, as a Result for the envelope."""
    command: str = args.command
    call = call_for(args)
    if command == "tasks" or (command == "wakes" and args.wakes_command is None):
        document = api.all_pages("/v1" + call.path)
    else:
        try:
            document = _request(api, call, reason=getattr(args, "reason", None))
        except ClientError as exc:
            if command == "republish" and exc.status == 404:
                exc.message = f"republish is unavailable on this Crucible server: {exc.message}"
                exc.hint = (
                    "either the task does not exist or the server predates republish; "
                    "read the task to tell which"
                )
            raise
    if command == "task":
        document = _related(api, args, document)
    if command == "health":
        return Result(kind="health", data=document, state=document.get("status"))
    if command == "tasks":
        return Result(kind="task_list", data=document, next=nx.task_list_actions(document, prefix))

    warnings: list[str] = []
    role: str | None
    if command in ORCHESTRATOR_ONLY:
        role = nx.PROBED_ORCHESTRATOR
    else:
        role, why = probe_role(api)
        if role is None:
            warnings.append(f"the principal's role is unknown ({why}); `next` is empty")
    if command == "wakes":
        kind = "wake" if args.wakes_command == "ack" else "wake_list"
        return Result(
            kind=kind,
            data=document,
            next=nx.wake_actions(document, role, prefix),
            warnings=warnings,
            role=role,
        )
    task = document["task"] if command == "task" and "task" in document else document
    return Result(
        kind="task_detail" if task is not document else "task",
        data=document,
        state=task.get("state") if isinstance(task, dict) else None,
        next=nx.task_actions(task, role, prefix),
        warnings=warnings,
        role=role,
    )
