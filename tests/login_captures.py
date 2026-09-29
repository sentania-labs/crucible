"""Each harness CLI's real login output, captured under a pty, and a stand-in that
replays it (hades #173).

The captures in tests/fixtures_data/logins are the raw bytes each CLI wrote to its
terminal in the pinned worker image (crucible-worker:20260916-f2e7118123e7: Claude Code
2.1.280, Codex 0.156.0, AGY 1.2.8), run exactly as the login driver runs it (`script
-qfec "stty cols 4096 rows 50; exec <cli> ..."`, TERM=xterm, the login's directory
variable set), with nothing typed, stopped at the prompt. No sign-in was completed. The
PKCE challenge, the OAuth state and Codex's one-time code are replaced with filler of
the same shape; everything else, escape sequences and carriage returns included, is
byte for byte what the CLI printed. `agy_timeout.raw` is AGY left alone past its own
60 seconds.

`replay_script` is a shell command that plays one capture back through whatever
terminal it runs in and then reads the code the way that CLI does: Claude Code in raw
mode, where only a carriage return ends the input; AGY a line in the terminal's normal
mode, echoing it; Codex nothing, finishing on its own as a device flow does.
"""

from __future__ import annotations

import base64
from pathlib import Path

CAPTURES = Path(__file__).parent / "fixtures_data" / "logins"


def capture(name: str) -> bytes:
    return (CAPTURES / f"{name}.raw").read_bytes()


# How each CLI reads the pasted code, from the capture's behaviour: Claude Code (Ink)
# puts the terminal in raw mode, AGY reads a line with echo on, Codex reads nothing.
_READERS = {
    # A byte at a time through dd: bash's own `read` puts the terminal back into a
    # mode that turns a carriage return into a newline, which Ink's raw mode never
    # does. A newline is kept as part of the code, as Ink keeps it.
    "claude_code": (
        "stty raw -echo; "
        "while :; do ch=$(dd bs=1 count=1 2>/dev/null; echo x); ch=${ch%x}; "
        '[ -z "$ch" ] && break; [ "$ch" = $\'\\r\' ] && break; code=$code$ch; done; '
        "stty sane -onlcr; printf '\\r\\n'; "
    ),
    "agy": "stty sane -onlcr; IFS= read -r code; ",
    "codex": "sleep 2; ",
}


def replay_script(name: str, *, harness: str | None = None, finish: str = "") -> str:
    """A bash script that replays capture `name`, reads the code as `harness`'s CLI
    does, reports how many characters it read, runs `finish` and exits 0. The pty
    already turned each `\\n` into `\\r\\n` when the capture was taken, so the replay
    turns that off rather than doubling it."""
    reader = _READERS[harness or name]
    payload = base64.b64encode(capture(name)).decode("ascii")
    return (
        "code=''; stty -onlcr 2>/dev/null; "
        f"printf '%s' '{payload}' | base64 -d; "
        f"{reader}"
        "stty sane 2>/dev/null; "
        'echo "stand-in read ${#code} characters"; '
        f"{finish}"
        "exit 0"
    )


def replay_to_exit(name: str, exit_code: int) -> str:
    """A capture that ends the CLI on its own (AGY's wait running out)."""
    payload = base64.b64encode(capture(name)).decode("ascii")
    return f"stty -onlcr 2>/dev/null; printf '%s' '{payload}' | base64 -d; exit {exit_code}"
