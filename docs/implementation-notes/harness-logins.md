# Harness logins on Kubernetes, and enabling a harness from the UI (hades #173, #174)

FDY-0132, 2026-09-28, on the operator's go of that evening.

## What the captures showed

Each CLI's login was run in the pinned worker image (crucible-worker:20260916-f2e7118123e7)
under `script`, exactly as the login driver runs it, with nothing typed. The raw bytes
are the fixtures in `tests/fixtures_data/logins`, with the PKCE challenge, the OAuth
state and Codex's one-time code replaced by filler of the same shape.

- Claude Code 2.1.280 ends every line `\r\r\n`, separates words with cursor-column
  moves, prints the URL as an OSC 8 hyperlink, and asks `Paste code here if prompted >`
  on a line of its own. It reads raw: a pasted code followed by a newline is echoed
  masked and never submitted; a carriage return submits it. Every login path sent a
  newline, so a Claude Code login could not have finished even once its output showed.
- AGY 1.2.8 prints the Google URL, `Waiting for authentication (timeout 60s)...` and
  `Or, paste the authorization code here and press Enter:`, reads a line in the
  terminal's normal mode (either Enter works), and after 60 seconds prints `Error:
  authentication timed out.` and exits 1. It refuses a percent-encoded code as
  malformed, which is how the address bar shows one.
- Codex 0.156.0 `login --device-auth` prints the URL and a one-time code and reads
  nothing.
- The login page offered Start only before any login had run, so a failed login could
  not be retried from the UI.

## Operator decisions

- 2026-09-27, hades #174: enabling a harness is one audited UI action; an unverified
  harness shows why and can be enabled anyway; the configuration entry becomes an
  initial default only. Recorded as ADR 0021.
- 2026-09-27, hades #173 (comment): recognise each harness's real prompt from captured
  output, show the code box whenever the CLI waits after printing a URL, drop the chmod
  noise, and tell the operator to copy AGY's `code` from the redirect that fails to load.

## Not proven here

No real sign-in was completed: there were no operator credentials, and the lab
deployment was out of bounds. The fixtures stop at each prompt; the kind tier replays
them through the real driver in a Pod.
