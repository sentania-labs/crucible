"""A ref a contract names is quoted wherever it is used, and refused if it is not a
plain ref (05). Both, not either."""

from __future__ import annotations

import pytest

from crucible.application.errors import ContractValidationError
from crucible.application.submit_task import parse_contract
from crucible.domain.refs import InvalidRefError, check_ref, ref_problem
from tests.fixtures import contract_document

GOOD = ["main", "crucible/FDY-0042", "release/1.2.3", "a_b-c.d", "feature/x/y"]
BAD = [
    "crucible/$(id)",
    "crucible/`id`",
    "crucible/a b",
    "crucible/a;id",
    "crucible/a'b",
    'crucible/a"b',
    "crucible/a|b",
    "crucible/a&b",
    "crucible/a\nb",
    "crucible/a\\b",
    "-delete-everything",
    "--upload-pack=touch /tmp/x",
    "/leading",
    "trailing/",
    "double//slash",
    "a..b",
    "a@{0}",
    "a.lock",
    ".hidden",
    "a/.hidden/b",
    "",
]


@pytest.mark.parametrize("value", GOOD)
def test_a_plain_ref_is_accepted(value: str) -> None:
    assert ref_problem(value) is None
    assert check_ref(value) == value


@pytest.mark.parametrize("value", BAD)
def test_anything_that_could_be_more_than_a_ref_is_refused(value: str) -> None:
    assert ref_problem(value) is not None
    with pytest.raises(InvalidRefError):
        check_ref(value, field="work_branch")


def test_a_ref_longer_than_git_allows_is_refused() -> None:
    assert ref_problem("a" * 256) is not None


def test_a_contract_with_a_command_substitution_work_branch_is_refused() -> None:
    """The submit path is where this is caught, before anything is ever run."""
    document = contract_document()
    document["repository"]["work_branch"] = "crucible/$(id)"
    with pytest.raises(ContractValidationError) as raised:
        parse_contract(document)
    assert any("work_branch" in p["path"] for p in raised.value.errors), raised.value.errors


def test_a_contract_with_an_option_shaped_base_ref_is_refused() -> None:
    document = contract_document()
    document["repository"]["base_ref"] = "--upload-pack=touch /tmp/x"
    with pytest.raises(ContractValidationError) as raised:
        parse_contract(document)
    assert any("base_ref" in p["path"] for p in raised.value.errors), raised.value.errors
