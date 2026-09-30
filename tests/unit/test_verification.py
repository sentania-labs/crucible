from typing import Any

import pytest

from crucible.domain.verification import task_specific_checks


@pytest.mark.parametrize("policy", [{}, {"repository": {"required_checks": []}}])
def test_task_specific_checks_normalize_whitespace_and_use_fallback(policy: dict[str, Any]) -> None:
    contract = {
        "required_verification": [
            {"command": command}
            for command in [
                " make\t lint\n",
                "make test",
                "make  test-unit",
                "make scan",
                " python3\t-m unittest tests.test_x\n",
            ]
        ]
    }
    assert task_specific_checks(contract, policy) == ["python3 -m unittest tests.test_x"]


def test_task_specific_checks_use_policy_named_commands() -> None:
    policy = {
        "repository": {
            "required_checks": [" uv run\t ruff check . ", "pytest tests", "custom scan"]
        }
    }
    contract = {
        "required_verification": [
            {"command": "uv  run ruff check ."},
            {"command": "pytest\n tests"},
            {"command": "custom scan"},
            {"command": "make test"},
            {"command": "pytest tests/test_x.py"},
        ]
    }
    assert task_specific_checks(contract, policy) == ["make test", "pytest tests/test_x.py"]


def test_task_specific_checks_require_a_nonempty_command() -> None:
    assert task_specific_checks({}, {}) == []
    assert (
        task_specific_checks(
            {
                "required_verification": [
                    {"kind": "artifact", "path": "report/evidence.md"},
                    {"command": " \n\t"},
                ]
            },
            {},
        )
        == []
    )
