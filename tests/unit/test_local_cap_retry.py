from crucible.application.supervisor import (
    local_cap_kind,
    retryable_exit,
    too_big_wake_summary,
)
from crucible.domain.exit_class import ExitClass


def test_a_local_turn_cap_blocks_instead_of_retrying() -> None:
    cap = local_cap_kind("local", ExitClass.COMPLETED_WITHOUT_REPORT, True)
    assert cap == "turns"
    assert f"too_big_for_local:{cap}" == "too_big_for_local:turns"


def test_a_local_time_cap_blocks_instead_of_retrying() -> None:
    cap = local_cap_kind("local", ExitClass.TIMEOUT, False)
    assert cap == "time"
    assert f"too_big_for_local:{cap}" == "too_big_for_local:time"


def test_a_frontier_time_cap_still_retries() -> None:
    assert local_cap_kind("subscription", ExitClass.TIMEOUT, False) is None
    assert retryable_exit(ExitClass.TIMEOUT, ["timeout"])


def test_the_too_big_wake_names_the_cap() -> None:
    assert too_big_wake_summary("turns") == "split the task: the local attempt hit its turn cap"
    assert too_big_wake_summary("time") == "split the task: the local attempt hit its time cap"
