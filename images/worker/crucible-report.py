#!/usr/bin/python3
"""crucible-report: check a worker's report.yaml before the worker exits (hades #215).

    crucible-report check /crucible/report/report.yaml [--contract PATH]

Prints each problem in plain words and exits 1 while there is any, 0 when there is
none, and 2 when it cannot run at all. It reads the contract from the identity bundle
(`/crucible/identity/contract.json`) to check that every acceptance criterion has an
entry. It needs no network and nothing beyond the image: the standard library, and
PyYAML from the Hermes venv, which it re-executes under when the system Python has no
YAML parser.

It mirrors CompletionClaimV1 in crucible/contracts/completion_claim.py, which is what
Crucible itself parses; tests/unit/test_report_check.py holds the two in agreement.
The worker writes judgement: summary, acceptance_mapping, the proposed pull request's
title and body, limitations, risks, blockers and follow_ups. Crucible fills the facts
(task_external_id, changed_files, refs, checks, run_evidence) from its own evidence, so
they are optional here.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the image's system Python
    yaml = None

HERMES_PYTHON = "/opt/hermes/bin/python3"
DEFAULT_CONTRACT = "/crucible/identity/contract.json"
REEXEC_MARK = "CRUCIBLE_REPORT_REEXEC"

FACT_FIELDS = ("task_external_id", "changed_files", "refs", "checks", "run_evidence")
JUDGEMENT_FIELDS = (
    "summary",
    "acceptance_mapping",
    "proposed_pull_request",
    "limitations",
    "risks",
    "blockers",
    "follow_ups",
)
LIST_FIELDS = ("limitations", "risks", "blockers", "follow_ups")
STATUSES = ("met", "not_met", "not_exercised", "partial")
MAPPING_KEYS = ("id", "status", "evidence")
PULL_REQUEST_KEYS = ("title", "body", "closes")
REFS_KEYS = ("branch", "head_sha", "commits")
CHECK_KEYS = ("id", "command", "exit", "log")
KNOWN = ("schema_version", *FACT_FIELDS, *JUDGEMENT_FIELDS)
VERSION = re.compile(r"^1\.[0-9]+$")

WHY_UNKNOWN = {
    "head": "Crucible reads the head from the collected branch",
    "head_sha": "Crucible reads the head from the collected branch",
    "base": "Crucible takes the base from the contract",
}


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fact_problem(name: str, value: Any) -> str | None:
    """What is wrong with a fact field the worker chose to write, or None."""
    if name == "task_external_id":
        return None if isinstance(value, str) and value else "it must be the task's id as text"
    if name in ("changed_files", "run_evidence"):
        return None if _is_str_list(value) else "it must be a list of paths"
    if name == "refs":
        if not isinstance(value, dict):
            return "it must be a mapping of branch, head_sha and commits"
        extra = sorted(str(k) for k in value if k not in REFS_KEYS)
        if extra:
            return f"it has fields that are not refs fields: {', '.join(extra)}"
        if not (isinstance(value.get("branch"), str) and value.get("branch")):
            return "refs.branch must be the branch name"
        if not (isinstance(value.get("head_sha"), str) and value.get("head_sha")):
            return "refs.head_sha must be the commit hash"
        if not (_is_int(value.get("commits")) and value["commits"] >= 0):
            return "refs.commits must be a whole number"
        return None
    if not isinstance(value, list):
        return "it must be a list of checks"
    for index, check in enumerate(value):
        if not isinstance(check, dict):
            return f"checks[{index}] must be a mapping of id, command, exit and log"
        extra = sorted(str(k) for k in check if k not in CHECK_KEYS)
        if extra:
            return f"checks[{index}] has fields that are not check fields: {', '.join(extra)}"
        for key in ("id", "command", "log"):
            if not (isinstance(check.get(key), str) and check.get(key)):
                return f"checks[{index}].{key} must be text"
        if not _is_int(check.get("exit")):
            return f"checks[{index}].exit must be the exit code, a whole number"
    return None


def _mapping_entries(value: Any, problems: list[str]) -> list[dict[str, Any]]:
    """The mapping's entries as a list, whichever of the two accepted forms it has."""
    if isinstance(value, dict):
        entries: list[Any] = []
        for key, entry in value.items():
            if isinstance(entry, dict):
                entries.append({"id": key, **{k: v for k, v in entry.items() if k != "id"}})
            elif isinstance(entry, str):
                entries.append({"id": key, "status": entry, "evidence": ""})
            else:
                entries.append(entry)
    elif isinstance(value, list):
        entries = list(value)
    else:
        problems.append(
            "acceptance_mapping must be a list of entries (id, status, evidence), or a "
            "mapping keyed by criterion id."
        )
        return []
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"acceptance_mapping entry {index + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping of id, status and evidence.")
            continue
        if isinstance(entry.get("id"), str) and entry["id"]:
            where = f"acceptance_mapping entry {entry['id']}"
        else:
            problems.append(f"{where} has no id: give the acceptance criterion's id, as text.")
        for key in sorted(str(k) for k in entry if k not in MAPPING_KEYS):
            problems.append(f"{where} has `{key}`, which is not a field: remove it.")
        if entry.get("status") not in STATUSES:
            problems.append(f"{where}: status must be one of {', '.join(STATUSES)}.")
        if not isinstance(entry.get("evidence"), str):
            problems.append(
                f"{where}: evidence must be text saying what shows it, for example the log "
                "file and what it shows."
            )
        out.append(entry)
    return out


def check(document: Any, criteria: list[str] | None) -> list[str]:
    """Every problem with the document, in plain words. Empty means none."""
    if not isinstance(document, dict):
        return ["The report must be a YAML mapping of field names to values."]
    problems: list[str] = []
    for key in sorted(str(k) for k in document if k not in KNOWN):
        why = WHY_UNKNOWN.get(key)
        problems.append(
            f"`{key}` is not a report field: remove it" + (f" ({why})." if why else ".")
        )

    version = document.get("schema_version")
    if version is None:
        problems.append('schema_version is missing: write `schema_version: "1.0"`.')
    elif not isinstance(version, str):
        problems.append('schema_version must be text: write it quoted, `schema_version: "1.0"`.')
    elif not VERSION.match(version):
        problems.append(
            'schema_version must be the format version "1.0", not the schema\'s name.'
        )

    summary = document.get("summary")
    if summary is None:
        problems.append("summary is missing: say in a few sentences what you changed and why.")
    elif not (isinstance(summary, str) and summary.strip()):
        problems.append("summary must be text saying what you changed and why.")

    if "acceptance_mapping" not in document:
        problems.append(
            "acceptance_mapping is missing: give each acceptance criterion an entry with "
            "its id, a status and the evidence."
        )
    else:
        entries = _mapping_entries(document["acceptance_mapping"], problems)
        mapped = {str(e.get("id")) for e in entries if isinstance(e.get("id"), str)}
        if criteria is not None:
            for criterion in criteria:
                if criterion not in mapped:
                    problems.append(
                        f"acceptance criterion {criterion} has no entry in acceptance_mapping."
                    )
            for extra in sorted(mapped - set(criteria)):
                problems.append(
                    f"acceptance_mapping has {extra}, which is not an acceptance criterion "
                    "of this contract (verification ids never go here): remove it."
                )

    pull_request = document.get("proposed_pull_request")
    if pull_request is None:
        problems.append(
            "proposed_pull_request is missing: give it a title and a body (closes is optional)."
        )
    elif not isinstance(pull_request, dict):
        problems.append("proposed_pull_request must be a mapping with a title and a body.")
    else:
        for key in sorted(str(k) for k in pull_request if k not in PULL_REQUEST_KEYS):
            problems.append(
                f"proposed_pull_request.{key} is not a field: remove it (Crucible sets the "
                "pull request's base and head itself)."
            )
        title = pull_request.get("title")
        if not (isinstance(title, str) and title):
            problems.append("proposed_pull_request.title must be a one-line title, as text.")
        if not isinstance(pull_request.get("body"), str):
            problems.append("proposed_pull_request.body must be text.")
        if "closes" in pull_request and not _is_str_list(pull_request["closes"]):
            problems.append("proposed_pull_request.closes must be a list of issue references.")

    for name in LIST_FIELDS:
        if name not in document:
            problems.append(f"{name} is missing: write a list, `[]` when there are none.")
        elif not _is_str_list(document[name]):
            problems.append(f"{name} must be a list of text items, `[]` when there are none.")

    for name in FACT_FIELDS:
        if document.get(name) is None:
            continue
        why = _fact_problem(name, document[name])
        if why:
            problems.append(
                f"{name} is optional, because Crucible fills it from its own evidence, but "
                f"as written it is not valid: {why}. Fix it or remove it."
            )
    return problems


def _criteria(contract_path: Path) -> list[str] | None:
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = contract.get("acceptance_criteria") if isinstance(contract, dict) else None
    if not isinstance(items, list):
        return None
    return [str(c.get("id")) for c in items if isinstance(c, dict) and c.get("id")]


def _load(path: Path) -> tuple[Any, str | None]:
    text = path.read_text(encoding="utf-8")
    if yaml is None:
        try:
            return json.loads(text), None
        except ValueError:
            return None, "no YAML parser here, and the report is not JSON either"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"the report is not valid YAML: {exc}"


def main(argv: list[str]) -> int:
    usage = "usage: crucible-report check <report.yaml> [--contract <contract.json>]"
    if len(argv) < 2 or argv[0] != "check":
        print(usage, file=sys.stderr)
        return 2
    if yaml is None and not os.environ.get(REEXEC_MARK) and os.access(HERMES_PYTHON, os.X_OK):
        # The system Python has no YAML parser; the Hermes venv's has PyYAML. -I keeps
        # the venv's site-packages to this one process and ignores the environment.
        os.environ[REEXEC_MARK] = "1"
        os.execv(HERMES_PYTHON, [HERMES_PYTHON, "-I", os.path.abspath(__file__), *argv])
    report = Path(argv[1])
    contract = Path(DEFAULT_CONTRACT)
    rest = argv[2:]
    if rest:
        if len(rest) != 2 or rest[0] != "--contract":
            print(usage, file=sys.stderr)
            return 2
        contract = Path(rest[1])
    if not report.is_file():
        print(f"{report}: no such file. Write the report there first.")
        return 1
    document, error = _load(report)
    if error:
        print(f"{report}: 1 problem")
        print(f"- {error}")
        return 1
    criteria = _criteria(contract)
    problems = check(document, criteria)
    if criteria is None:
        print(f"note: no contract at {contract}, so acceptance criteria coverage is not checked")
    if not problems:
        print(
            f"{report}: no problems. Crucible fills {', '.join(FACT_FIELDS)} from its own "
            "evidence."
        )
        return 0
    print(f"{report}: {len(problems)} problem{'s' if len(problems) != 1 else ''}")
    for problem in problems:
        print(f"- {problem}")
    print(f"Fix each one and run `crucible-report check {report}` again before you exit 0.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
