# S1b: Do Crucible's dedicated harness credential sessions leave the operator's daily sessions valid?

Run by Foundry with the operator present, 2026-09-16, 16:20 to 17:21 CDT,
on the reference workstation. No credential was copied from the operator's
directories, no token value was recorded or compared, and every scratch
copy used for a container run was removed. Hashes below are SHA-256
prefixes of whole files, used only to show change or no change.

Result: **pass on the steps that could be exercised; Claude Code verified,
Codex and AGY pending their Crucible-side refresh.** The dedicated sessions did not invalidate the operator's
sessions, including across a host-side AGY refresh and a host-side Codex
refresh observed earlier the same day. The Crucible-side refresh of a
dedicated Codex session (steps 5 and 6) has not yet occurred naturally and
is re-checked when it does; Claude Code's long-lived token has no refresh.

## Method

Per harness: (1) confirm the operator's normal session answers a trivial
prompt; (2) log in directly into Crucible's dedicated directory with the
harness's own interactive flow and its home or config variable pointed at
that directory; (3) run a trivial prompt through the dedicated credential
inside the hardened worker image (uid 1000, all capabilities dropped,
no-new-privileges, read-only root, tmpfs home, `--init`), from a
per-attempt copy owned by uid 1000 seeded with only the auth file; (4)
confirm the operator's session again; (5) observe a Crucible-side refresh
where practical; (6) confirm the operator's session after it.

The interactive logins were driven headlessly through a pseudo-terminal
driver that shows the operator the URL, feeds the code the operator
pastes back, and writes any resulting token to a mode 600 file without
displaying it. The operator completed each flow from another machine.

## Codex (0.153.4 in the image; host CLI reported 0.154.0 after updating itself during the day)

| Step | Time | Result |
|---|---|---|
| 1 operator session | 14:28 | `codex exec` OK; the host refreshed its own auth file at that moment (`last_refresh` updated) |
| 2 dedicated login | 17:08 | `CODEX_HOME=<dedicated> codex login --device-auth`; device code entered by the operator; `auth.json` written, `auth_mode` chatgpt |
| 3 worker prompt | 17:09:05 | exit 0, `OK`; per-attempt copy seeded with `auth.json` only; the CLI wrote its usual session state beside it; auth file unchanged |
| 4 operator session | 17:09:11 | `codex exec` OK; host auth file unchanged since 14:28 |
| 5 and 6 refresh | pending | the dedicated access token has not expired yet; re-run steps 3, 4 after it does |

Two things learned: a direct mount of the dedicated directory fails
(`Permission denied` on `config.toml`) because the directory is owned by
the service user with mode 700 and the worker is uid 1000, which is why
the spec's per-attempt copy exists; and a device-code login expires in 15
minutes, which the onboarding command must state up front.

## AGY (1.2.4)

| Step | Time | Result |
|---|---|---|
| 1 operator session | 16:20 | `agy -p` OK |
| 2 dedicated login | 17:19:15 to 17:19:44 | `HOME=<dedicated> agy -p ...` prints a Google OAuth URL and waits **60 seconds** for a pasted code; the first two attempts timed out on the round trip; the third succeeded with the operator ready; token written under `.gemini/antigravity-cli/` |
| 3 worker prompt | 17:20:39 | exit 0, `OK`; per-attempt copy of the token file only; token unchanged |
| 4 operator session | 17:20:43 | `agy -p` OK; the host token file **changed** (the host session refreshed itself) |
| 5 and 6 refresh | pending | the host refresh after the dedicated login succeeded, which is the opposite direction; a Crucible-side refresh is expected on the dedicated session's first run after its one-hour expiry, and steps 3 and 4 are re-run then |

The 60-second window is the onboarding constraint for AGY: the admin
command must tell the operator to have the browser signed in first.

## Claude Code (2.1.273)

| Step | Time | Result |
|---|---|---|
| 1 operator session | 16:20 | `claude -p` OK |
| 2 dedicated login | 17:14 to 17:20 | `CLAUDE_CONFIG_DIR=<dedicated> claude setup-token`; the operator approved in a browser and pasted the code; the resulting long-lived token was captured to `oauth-token` (mode 600) by the driver and never displayed |
| 3 worker prompt | 17:21:08 | exit 0, `OK`, token delivered through the CLI's documented environment variable |
| 4 operator session | 17:21:12 | `claude -p` OK; host credentials file unchanged |
| 5 and 6 refresh | not applicable | the long-lived token does not refresh |

`setup-token` prints the token once for the operator to store; it does not
write a credentials file. Crucible's onboarding keeps it as a file the
launch reads into the environment at container start, which spec 07
already names as the one exception to file-only delivery.

## Simultaneous sessions per subscription account

Observed, not documented by the providers: all three accepted a second
authenticated session on the same account while the operator's own
session stayed valid, including across a host-side refresh for AGY and
Codex. Whether a provider limits the number of sessions or revokes the
oldest is not established by this test.

## Decision

Proceed. `session_compatibility` may be set to `verified` for Claude Code
now (no refresh exists). AGY and Codex stay `unverified` until their
dedicated sessions have refreshed on the Crucible side and the operator's
session is confirmed after that refresh; the host-side refreshes observed
today show only that the operator's session can refresh after the
dedicated login, not that a Crucible-side refresh leaves it valid. Onboarding must state
the device-code expiry (Codex), the 60-second window (AGY), and the
one-time display of the long-lived token (Claude Code).
