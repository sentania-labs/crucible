"""hades #222: the kind tier runs as shards in CI; no kind test may run nowhere or twice."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
KIND_TESTS = ROOT / "tests" / "e2e" / "test_kind.py"
SHARDS = ROOT / "tools" / "kind" / "shards"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _kind_test_functions() -> list[str]:
    tree = ast.parse(KIND_TESTS.read_text())
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _shard_lists() -> dict[str, list[str]]:
    lists: dict[str, list[str]] = {}
    for path in sorted(SHARDS.glob("*.txt")):
        lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        lists[path.stem] = lines
    return lists


def test_every_kind_test_is_in_exactly_one_shard() -> None:
    functions = _kind_test_functions()
    listed: Counter[str] = Counter()
    for shard, lines in _shard_lists().items():
        assert lines, f"shard {shard} is empty"
        for line in lines:
            assert line.startswith("tests/e2e/test_kind.py::"), f"shard {shard}: {line}"
            listed[line.split("::", 1)[1]] += 1
    missing = sorted(set(functions) - set(listed))
    unknown = sorted(set(listed) - set(functions))
    twice = sorted(name for name, count in listed.items() if count > 1)
    assert not missing, f"kind tests in no shard: {missing}"
    assert not unknown, f"shard entries that are not kind tests: {unknown}"
    assert not twice, f"kind tests in more than one shard: {twice}"


def test_the_workflow_runs_the_shards_and_not_main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    kind = document["jobs"]["e2e-kind"]
    shards = kind["strategy"]["matrix"]["shard"]
    assert sorted(str(s) for s in shards) == sorted(_shard_lists())
    assert kind["env"]["CRUCIBLE_E2E_KIND_SHARD"] == "${{ matrix.shard }}"
    assert "e2e-kind" in document["jobs"]["green"]["needs"]
    branches = document[True]["push"]["branches"]  # `on` parses as the boolean True
    assert "!main" in branches
