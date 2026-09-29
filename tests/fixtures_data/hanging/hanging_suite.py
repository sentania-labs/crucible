"""A deliberately hanging test (issue 192). tests/unit/test_test_time_limit.py copies this
file into a scratch directory as a test module and runs it under the repository's pytest
configuration. It is named so the real suite never collects it."""

import threading


def test_hangs_forever() -> None:
    # A helper thread stuck as well, so the dump has to show more than the main thread.
    helper = threading.Thread(
        target=threading.Event().wait, name="stuck-helper-thread", daemon=True
    )
    helper.start()
    threading.Event().wait()


def test_runs_after_the_hang() -> None:
    assert True
