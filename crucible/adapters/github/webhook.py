"""The optional webhook accelerator (04, 23).

Off by default on a workstation. Polling is the complete observation path; a delivery
only shortens latency, so nothing here may be the only way a fact arrives.

What a delivery goes through, in order: verify the HMAC against the raw body in memory;
reject and count on a mismatch, storing nothing; parse; extract only the fields Crucible
uses; scan and redact every user-controlled text field; keep the delivery id, event,
action, those fields, and a SHA-256 of the original body; discard the raw body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from crucible.adapters.github import normalize

SIGNATURE_HEADER = "X-Hub-Signature-256"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

HANDLED_EVENTS: frozenset[str] = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_run",
        "check_suite",
        "workflow_run",
        "push",
    }
)


class SignatureError(Exception):
    """The delivery is unsigned or the signature does not match. Nothing is stored."""


@dataclass(frozen=True, slots=True)
class NormalizedDelivery:
    delivery_id: str
    event: str
    action: str
    repository: str
    body_sha256: str
    normalized: dict[str, Any] = field(default_factory=dict)
    # Whether this delivery is about a subject whose reactions should be polled at once
    # (23: a review or comment delivery triggers an immediate reaction poll).
    triggers_reaction_poll: bool = False


def verify_signature(secret: str, raw_body: bytes, header: str | None) -> None:
    """Constant-time HMAC-SHA256 over the raw body. Raises on anything but a match."""
    if not secret:
        raise SignatureError("no webhook secret is configured; deliveries are refused")
    if not header or not header.startswith("sha256="):
        raise SignatureError("the delivery carries no sha256 signature")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header[len("sha256=") :].strip()):
        raise SignatureError("the delivery signature does not match the body")


def body_digest(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def normalize_delivery(
    *, delivery_id: str, event: str, raw_body: bytes
) -> NormalizedDelivery | None:
    """The normalized record, or None for an event Crucible does not handle.

    The raw body is parsed here and does not leave this function."""
    if event not in HANDLED_EVENTS:
        return None
    digest = body_digest(raw_body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    repository = ""
    repo_node = payload.get("repository")
    if isinstance(repo_node, dict):
        repository = str(repo_node.get("full_name", ""))
    action = str(payload.get("action", ""))
    fields: dict[str, Any] = {}
    triggers = False
    pr_node = payload.get("pull_request")
    if isinstance(pr_node, dict):
        pr = normalize.pull_request(pr_node)
        fields["pull_request"] = {
            "number": pr.number,
            "head_sha": pr.head_sha,
            "state": pr.state,
            "merged": pr.merged,
            "merged_by": pr.merged_by,
            "merge_commit_sha": pr.merge_commit_sha,
            "base_ref": pr.base_ref,
            "url": pr.url,
        }
    if event == "pull_request_review" and isinstance(payload.get("review"), dict):
        record = normalize.review(payload["review"])
        fields["review"] = {
            "github_id": record.github_id,
            "login": record.login,
            "state": record.state,
            "body": record.body,
            "commit_id": record.commit_id,
            "submitted_at": record.submitted_at.isoformat(),
        }
        triggers = True
    if event == "pull_request_review_comment" and isinstance(payload.get("comment"), dict):
        record_c = normalize.review_comment(payload["comment"])
        fields["comment"] = _comment_fields(record_c)
        triggers = True
    if event == "issue_comment" and isinstance(payload.get("comment"), dict):
        record_c = normalize.issue_comment(payload["comment"])
        fields["comment"] = _comment_fields(record_c)
        issue = payload.get("issue")
        if isinstance(issue, dict):
            fields["issue_number"] = int(issue.get("number", 0))
            fields["is_pull_request"] = bool(issue.get("pull_request"))
        triggers = True
    for key, fn in (
        ("check_run", normalize.check_run),
        ("check_suite", normalize.check_suite),
        ("workflow_run", normalize.workflow_run),
    ):
        node = payload.get(key)
        if event == key and isinstance(node, dict):
            check = fn(node)
            fields["check"] = {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "head_sha": check.head_sha,
                "url": check.url,
                "external_id": check.external_id,
                "workflow": check.workflow,
                "source": check.source,
            }
    if event == "push":
        fields["ref"] = str(payload.get("ref", ""))
        fields["after"] = str(payload.get("after", ""))
        fields["before"] = str(payload.get("before", ""))
        pusher = payload.get("pusher")
        if isinstance(pusher, dict):
            fields["pusher"] = str(pusher.get("name", ""))
    sender = payload.get("sender")
    if isinstance(sender, dict):
        fields["sender"] = str(sender.get("login", ""))
    return NormalizedDelivery(
        delivery_id=delivery_id,
        event=event,
        action=action,
        repository=repository,
        body_sha256=digest,
        normalized=fields,
        triggers_reaction_poll=triggers,
    )


def _comment_fields(record: Any) -> dict[str, Any]:
    return {
        "github_id": record.github_id,
        "login": record.login,
        "body": record.body,
        "kind": record.kind,
        "path": record.path,
        "line": record.line,
        "commit_id": record.commit_id,
        "review_id": record.review_id,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
