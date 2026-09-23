"""`next`: the actions valid from a record's state for the principal in use.

Each action names the command that performs it as an argv list, with `{placeholder}`
tokens for what the caller must supply, described in `needs`; `optional` lists flags the
caller may add; `requires` says which roles the API accepts it from and, for a task,
whose task it is (an orchestrator acts only on its own tasks, 04). The rules mirror the
API's own checks (the lifecycle table of 09 and the state guards of each service), so an
action is offered only where the API would take it on the state alone. What the state
cannot show (a live supervisor lease, a matching review comment, a prepared directory)
is still the API's to refuse, and the refusal comes back in the envelope.

The CLI never gates on its own: `next` is advice. An unknown role offers nothing rather
than a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from crucible.domain.entities import AcceptanceVerdict, CIAction, CICause, HeadAction

ADMIN = "admin"
ORCHESTRATOR = "orchestrator"
OPERATOR = "operator"
OBSERVER = "observer"
ROLES = (ADMIN, ORCHESTRATOR, OPERATOR, OBSERVER)

# The route guards of 04 (crucible/adapters/api/deps.py), by the roles they admit.
ORCHESTRATOR_ROUTE = (ORCHESTRATOR, OPERATOR)
MUTATOR_ROUTE = (ORCHESTRATOR, OPERATOR, ADMIN)

# A probe cannot tell an operator from an orchestrator (both pass the orchestrator guard,
# neither passes the admin guard), so the client reports the pair as `orchestrator` and
# offers only what both may do.
PROBED_ORCHESTRATOR = ORCHESTRATOR

REASON = {"reason": "why; recorded with the operation (never a secret)"}

# 09's cancellable states plus `running`, which cancels through `cancelling`.
CANCELLABLE = frozenset(
    {
        "submitted",
        "scheduled",
        "running",
        "blocked",
        "awaiting_quota",
        "awaiting_internal_review",
        "awaiting_acceptance",
        "pre_pr_gates_failed",
        "publish_failed",
        "awaiting_external_review",
        "external_feedback_received",
        "awaiting_ci_certification",
        "ci_certification_failed",
        "head_diverged",
        "ready_for_merge",
    }
)
# crucible/application/corrections.py CORRECTABLE_STATES.
CORRECTABLE = frozenset(
    {
        "pre_pr_gates_failed",
        "awaiting_acceptance",
        "external_feedback_received",
        "ci_certification_failed",
    }
)
CLOSABLE = frozenset({"accepted", "merged", "released"})


def action(
    name: str,
    description: str,
    command: Sequence[str],
    *,
    needs: dict[str, Any] | None = None,
    optional: list[dict[str, Any]] | None = None,
    roles: Iterable[str] = (),
    owner: str | None = None,
) -> dict[str, Any]:
    requires: dict[str, Any] = {"roles": list(roles)}
    if owner is not None:
        requires["owner"] = owner
    entry: dict[str, Any] = {
        "action": name,
        "description": description,
        "command": list(command),
        "needs": needs or {},
        "requires": requires,
    }
    if optional:
        entry["optional"] = optional
    return entry


def _allowed(role: str | None, roles: Sequence[str]) -> bool:
    return role is not None and role in roles


# ----- the orchestrator verbs ----------------------------------------------------------


def task_actions(task: Any, role: str | None, prefix: Sequence[str]) -> list[dict[str, Any]]:
    """What the principal may do to this task from its state, as top-level verbs."""
    if not isinstance(task, dict) or not isinstance(task.get("state"), str):
        return []
    state = task["state"]
    tid = str(task.get("id", "{task_id}"))
    owner = task.get("principal") if isinstance(task.get("principal"), str) else None
    p = list(prefix)
    out: list[dict[str, Any]] = []

    def offer(roles: Sequence[str], entry: dict[str, Any]) -> None:
        if _allowed(role, roles):
            out.append(entry)

    if state == "submitted":
        policy = task.get("policy")
        version = policy.get("version") if isinstance(policy, dict) else None
        offer(
            MUTATOR_ROUTE,
            action(
                "start",
                "dispatch the submitted task; routing options must match the contract",
                [*p, "start", tid, "--policy-version", str(version), "--reason", "{reason}"],
                needs=REASON,
                optional=[
                    {"flag": f"--{name}", "description": "must equal the contract's value"}
                    for name in ("harness", "model", "provider", "image", "effort")
                ],
                roles=MUTATOR_ROUTE,
                # start_task checks no owner: any principal that may mutate may start.
            ),
        )
    if state == "awaiting_internal_review":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "review",
                "request the internal review round",
                [*p, "review", tid, "{file}", "--reason", "{reason}"],
                needs={"file": "path to a ReviewRequest JSON document", **REASON},
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state == "awaiting_acceptance":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "accept",
                "record the acceptance decision",
                [*p, "accept", tid, "--verdict", "{verdict}", "--reason", "{reason}"],
                needs={"verdict": {"choices": [v.value for v in AcceptanceVerdict]}, **REASON},
                optional=[{"flag": "--head-sha", "description": "the head the verdict is for"}],
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state in CORRECTABLE:
        offer(
            MUTATOR_ROUTE,
            action(
                "corrections",
                "attach a correction contract and send the task back to work",
                [*p, "corrections", tid, "{file}", "--reason", "{reason}"],
                needs={"file": "path to a correction JSON document", **REASON},
                roles=MUTATOR_ROUTE,
                owner=owner,
            ),
        )
    if state == "external_feedback_received":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "dispositions",
                "record a disposition for a review comment on the pull request",
                [*p, "dispositions", tid, "{file}", "--reason", "{reason}"],
                needs={"file": "path to a ReviewDisposition JSON document", **REASON},
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state == "ci_certification_failed":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "ci-decision",
                "decide the failed CI certification",
                [
                    *p,
                    "ci-decision",
                    tid,
                    "--cause",
                    "{cause}",
                    "--action",
                    "{ci_action}",
                    "--reason",
                    "{reason}",
                ],
                needs={
                    "cause": {"choices": [c.value for c in CICause]},
                    "ci_action": {"choices": [a.value for a in CIAction]},
                    **REASON,
                },
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state == "head_diverged":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "head-decision",
                "decide the divergent head",
                [*p, "head-decision", tid, "--action", "{head_action}", "--reason", "{reason}"],
                needs={"head_action": {"choices": [a.value for a in HeadAction]}, **REASON},
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state == "publish_failed":
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "republish",
                "retry the failed publication once",
                [*p, "republish", tid, "--reason", "{reason}"],
                needs=REASON,
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    if state in CLOSABLE:
        offer(
            ORCHESTRATOR_ROUTE,
            action(
                "close",
                "close the finished task",
                [*p, "close", tid, "--reason", "{reason}"],
                needs=REASON,
                roles=ORCHESTRATOR_ROUTE,
                owner=owner,
            ),
        )
    escalations = task.get("open_escalations")
    for escalation in escalations if isinstance(escalations, list) else []:
        if not isinstance(escalation, dict) or escalation.get("state") != "open":
            continue
        command = [
            *p,
            "decisions",
            tid,
            "--kind",
            "{kind}",
            "--verbatim",
            "{verbatim}",
            "--resolves",
            "{resolves}",
            "--escalation-id",
            str(escalation.get("id")),
            "--reason",
            "{reason}",
        ]
        offer(
            MUTATOR_ROUTE,
            action(
                "decisions",
                f"answer the open escalation: {escalation.get('question', '')}".strip(),
                command,
                needs={
                    "kind": "the decision kind (release_authorization is operator-only)",
                    "verbatim": "the deciding person's own words",
                    "resolves": "what the decision resolves",
                    **REASON,
                },
                optional=(
                    [{"flag": "--reschedule", "description": "send the blocked task back to work"}]
                    if state == "blocked"
                    else None
                ),
                roles=MUTATOR_ROUTE,
                owner=owner,
            ),
        )
    if state in CANCELLABLE:
        offer(
            MUTATOR_ROUTE,
            action(
                "cancel",
                "cancel the task",
                [*p, "cancel", tid, "--verbatim", "{verbatim}", "--reason", "{reason}"],
                needs={"verbatim": "the deciding person's own words", **REASON},
                optional=[{"flag": "--decided-by", "description": "who decided (default foundry)"}],
                roles=MUTATOR_ROUTE,
                owner=owner,
            ),
        )
    return out


def task_list_actions(document: Any, prefix: Sequence[str]) -> list[dict[str, Any]]:
    items = document.get("items") if isinstance(document, dict) else None
    if not items:
        return []
    return [
        action(
            "show",
            "read one task and the actions valid from its state",
            [*prefix, "task", "{task_id}"],
            needs={"task_id": "an `id` from data.items"},
            optional=[
                {"flag": f"--{flag}", "description": f"include the task's {flag.replace('-', ' ')}"}
                for flag in ("events", "pull-request", "attempts", "gates", "report", "evidence")
            ],
            roles=ROLES,
        )
    ]


def wake_actions(document: Any, role: str | None, prefix: Sequence[str]) -> list[dict[str, Any]]:
    items = document.get("items") if isinstance(document, dict) else [document]
    items = [item for item in items or [] if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    pending = [item for item in items if item.get("acked_at") is None]
    if pending and _allowed(role, MUTATOR_ROUTE):
        single = len(items) == 1 and not (isinstance(document, dict) and "items" in document)
        wake_id = str(items[0].get("id")) if single else "{wake_id}"
        out.append(
            action(
                "ack",
                "acknowledge the wake with what was done about it",
                [*prefix, "wakes", "ack", wake_id, "--reason", "{reason}"],
                needs=(
                    REASON
                    if single
                    else {"wake_id": "an `id` from data.items whose acked_at is null", **REASON}
                ),
                roles=MUTATOR_ROUTE,
            )
        )
    if any(item.get("task_id") for item in items):
        out.append(
            action(
                "show-task",
                "read the task the wake is about",
                [*prefix, "task", "{task_id}"],
                needs={"task_id": "a `task_id` from the wake"},
                roles=ROLES,
            )
        )
    return out


# ----- the admin group -----------------------------------------------------------------

CREDENTIAL_ACTIONS: dict[str, tuple[str, ...]] = {
    "absent": ("rotate", "login"),
    "configured": ("validate", "probe", "rotate", "remove", "login"),
    "invalid": ("validate", "rotate", "remove", "login"),
    "validated": ("probe", "rotate", "remove", "login"),
}
CREDENTIAL_WORDS = {
    "set": "read an API key from stdin or a hidden prompt; never placed in argv",
    "validate": "check the auth files' shape, then run the bounded probe",
    "probe": "run the bounded probe of the hardened image",
    "rotate": "copy a prepared credential directory in",
    "remove": "retain and shred the credential",
    "login": "run the harness's own login (on this host, or in the worker image remotely)",
}

# `set` is the LiteLLM virtual key path; only hermes takes one (`--harness` is
# restricted to it on the parser), so it is offered only for that harness.
SET_KEY_HARNESSES = frozenset({"hermes"})


def credential_actions(
    harness: str, state: str | None, prefix: Sequence[str]
) -> list[dict[str, Any]]:
    """By the credential's state (25). `login` runs the harness's own CLI: locally,
    directly on this host; remotely, in the promoted worker image
    (crucible/adapters/api/routers/admin.py `admin_login`), so it is offered in both
    modes on the state alone, and a live promoted image or an installed CLI is still
    the API's to refuse. With a credential already present it needs `--replace`. `set`
    is offered only for the harnesses that take an API key rather than a login."""
    out: list[dict[str, Any]] = []
    verbs = CREDENTIAL_ACTIONS.get(state or "", ())
    if harness in SET_KEY_HARNESSES and state in ("absent", "invalid"):
        verbs = ("set", *verbs)
    for verb in verbs:
        command = [*prefix, "--reason", "{reason}", "credentials", verb, "--harness", harness]
        needs: dict[str, Any] = dict(REASON)
        if verb == "rotate":
            command += ["--new-path", "{new_path}"]
            needs["new_path"] = "a prepared credential directory; left untouched"
        if verb == "login" and state != "absent":
            command.append("--replace")
        out.append(action(verb, CREDENTIAL_WORDS[verb], command, needs=needs, roles=(ADMIN,)))
    return out


def _labels_text(labels: Any) -> str:
    if not isinstance(labels, dict):
        return ""
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def kubernetes_egress_actions(document: Any, prefix: Sequence[str]) -> list[dict[str, Any]]:
    """One action: replace the setting, prefilled with what is in force now, so the
    command as offered changes nothing until a value in it is edited."""
    if not isinstance(document, dict) or not isinstance(document.get("document"), dict):
        return []
    current = document["document"]
    dns = current.get("dns") if isinstance(current.get("dns"), dict) else {}
    endpoint = (
        current.get("local_endpoint") if isinstance(current.get("local_endpoint"), dict) else {}
    )
    return [
        action(
            "set-egress",
            "replace the resolver's and the in-cluster local endpoint's selectors",
            [
                *prefix,
                "--reason",
                "{reason}",
                "kubernetes",
                "set-egress",
                f"--dns-namespace={dns.get('namespace', '')}",
                f"--dns-labels={_labels_text(dns.get('pod_labels'))}",
                f"--endpoint-namespace={endpoint.get('namespace', '')}",
                f"--endpoint-labels={_labels_text(endpoint.get('pod_labels'))}",
                f"--endpoint-port={endpoint.get('port', 0)}",
            ],
            needs=REASON,
            roles=(ADMIN,),
        )
    ]


def local_endpoint_actions(document: Any, prefix: Sequence[str]) -> list[dict[str, Any]]:
    """One action per model in data.models, already set to flip its current state:
    `enable` for a disabled model, `disable` for an enabled one. The parser's
    `--enable`/`--disable` are a required mutually exclusive pair, so the flag is baked
    into the command rather than advertised as an appendable option: an `--enable`
    already in the command and an offered `--disable` are a usage error together."""
    if not isinstance(document, dict):
        return []
    models = document.get("models")
    if not isinstance(models, list):
        return []
    out: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, dict) or not model.get("id"):
            continue
        model_id = str(model["id"])
        verb = "disable" if model.get("enabled") is True else "enable"
        out.append(
            action(
                f"set-local-endpoint:{model_id}",
                f"{verb} the {model_id} model on the local endpoint",
                [
                    *prefix,
                    "--reason",
                    "{reason}",
                    "routing",
                    "set-local-endpoint",
                    "--endpoint-url",
                    "{endpoint_url}",
                    f"--model={model_id}",
                    f"--{verb}",
                ],
                needs={"endpoint_url": "the local endpoint's base URL", **REASON},
                optional=[
                    {
                        "flag": "--enable-thinking",
                        "description": "turn on the model's thinking mode",
                    },
                    {
                        "flag": "--max-concurrency",
                        "description": "the pool's concurrency (default 4)",
                    },
                ],
                roles=(ADMIN,),
            )
        )
    return out


def harness_actions(items: Iterable[Any], prefix: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("harness"))
        on = item.get("enabled_by_administrator", item.get("enabled"))
        verb = "disable" if on else "enable"
        out.append(
            action(
                f"{verb}:{name}",
                f"{verb} the {name} harness for new launches",
                [*prefix, "--reason", "{reason}", "harnesses", verb, name],
                needs=REASON,
                roles=(ADMIN,),
            )
        )
    return out


def image_actions(items: Iterable[Any], prefix: Sequence[str]) -> list[dict[str, Any]]:
    return [
        action(
            f"promote:{item['digest']}",
            f"promote {item.get('reference', item['digest'])}",
            [*prefix, "--reason", "{reason}", "images", "promote", str(item["digest"])],
            needs=REASON,
            roles=(ADMIN,),
        )
        for item in items
        if isinstance(item, dict)
        and item.get("digest")
        and item.get("harnesses")
        and item.get("supported") is True
        and item.get("promotion_state") == "candidate"
    ]


def exhaustion_actions(items: Iterable[Any], prefix: Sequence[str]) -> list[dict[str, Any]]:
    return [
        action(
            f"clear-exhaustion:{item['pool']}",
            f"clear the exhaustion mark on pool {item['pool']}",
            [*prefix, "--reason", "{reason}", "routing", "clear-exhaustion", str(item["pool"])],
            needs=REASON,
            roles=(ADMIN,),
        )
        for item in items
        if isinstance(item, dict) and item.get("active") is True and item.get("pool")
    ]


def token_actions(items: Iterable[Any], prefix: Sequence[str]) -> list[dict[str, Any]]:
    live = [i for i in items if isinstance(i, dict) and i.get("disabled_at") is None]
    if not live:
        return []
    return [
        action(
            "revoke",
            "disable a principal's token",
            [*prefix, "--reason", "{reason}", "token", "revoke", "{principal_id}"],
            needs={"principal_id": "an `id` from data.items whose disabled_at is null", **REASON},
            roles=(ADMIN,),
        )
    ]


def bootstrap_actions(document: Any, prefix: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or document.get("state") != "verified":
        return []
    import_id = document.get("import_id")
    if not import_id:
        return []
    return [
        action(
            "commit",
            "make the verified import authoritative",
            [*prefix, "--reason", "{reason}", "bootstrap", "commit", str(import_id)],
            needs=REASON,
            roles=(ADMIN,),
        )
    ]


def audit_actions(
    document: Any, prefix: Sequence[str], limit: int, cursor: int | None
) -> list[dict[str, Any]]:
    """The next page while the scan still moves: `next_cursor` is how far it reached, so a
    page that reached no further is the end."""
    reached = document.get("next_cursor") if isinstance(document, dict) else None
    if not isinstance(reached, int) or reached == (cursor or 0):
        return []
    return [
        action(
            "next-page",
            "read the next page of the audit log",
            [*prefix, "audit", "tail", "--cursor", str(reached), "--limit", str(limit)],
            roles=(ADMIN,),
        )
    ]
