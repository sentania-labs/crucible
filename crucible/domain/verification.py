"""Task checks beyond the repository checks required by policy."""

from typing import Any

_GENERIC_CHECKS = {"make lint", "make test", "make test-unit", "make scan"}


def task_specific_checks(contract: dict[str, Any], policy_document: dict[str, Any]) -> list[str]:
    """Return normalized command checks not named in repository.required_checks.

    Those policy checks are mandatory in every contract and are re-run by the
    verification_ran gate. Older policies without commands use the standard set.
    Artifact requirements and empty commands are not executable checks.
    """
    named = policy_document.get("repository", {}).get("required_checks", [])
    generic = {" ".join(command.split()) for command in named if command.strip()}
    generic = generic or _GENERIC_CHECKS
    commands = (
        " ".join(check.get("command", "").split())
        for check in contract.get("required_verification", [])
        if check.get("kind", "command") == "command"
    )
    return [command for command in commands if command and command not in generic]
