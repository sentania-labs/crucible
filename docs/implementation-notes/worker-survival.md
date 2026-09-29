# Workers do not die or lose work, and are told less (FDY-0140)

The operator's direction of 2026-09-29: stop micromanaging workers; review is the
enforcement; Crucible does anything mechanical; hard failures only where the damage is
real or a claim is false; Hermes on the local model is the default worker. What follows
is what that meant here, and the choices made on the way.

## A silent Hermes run was stall-killed on Kubernetes

Activity was log bytes, workspace file changes, or a command in flight. Hermes `-z`
writes nothing while it works, and the file check walked the workspace path, which on
Kubernetes is a `k8s://` name, so it never saw a change. The Kubernetes provider now
answers `activity(handle, workspace)` by running a read-only `find` in the live worker
over the checkout, the report directory and the home, where Hermes updates its session
store after every turn. The supervisor asks a provider that has this method instead of
walking, no more often than a `command_running` renewal. The Hermes wrapper also writes a
line to stderr whenever that session store changes, which is log activity on every
provider, Docker included.

A stall is now recorded as the exit class `stalled`, not `timeout`. It behaves as
`timeout` did (no retry, gates on what exists) and wakes Foundry as `timed_out`: the wake
reasons are Foundry's contract and were left alone.

## Work left uncommitted is committed at collection

The collector commits what the worker left uncommitted, as the policy's `git` author
with the attempt trailer, before it bundles `base..work_branch`. The checkout is
therefore mounted writable into the collector on both providers. It is the quota
checkpoint's commit, with its guards (the worker's `.git/config` replaced, an empty hooks
directory); where the checkpoint refuses an unsafe `.git` and fails the collection, the
ordinary case skips the commit with a note and collects what was committed. Caches and
build output a repository forgot to ignore are never swept in, so a model that ran the
tests does not fail the scope gate on `__pycache__`. `attempt_collected` says when
Crucible made that commit, so the review knows whose it is.

## "I'm stuck" works with every harness

`blocked.md` on a clean exit is `blocked` whatever the exit code and whatever the report
says. For Hermes a clean exit is 0 only: its own 75 is a provider failure, so `blocked.md`
beside a Hermes 75 is still that failure.

## IDENTITY.md

Rewritten to about 300 words: the task, scope, what to read, the acceptance criteria, the
checks, the report and `crucible-report check`, and how to stop. Exit codes, the
precedence list, the author line and log capture are gone; the rendering bugs (Python
reprs, `prohibited_actions` read from `scope`, `Repository: (unset)`, escalation
conditions and context never shown, corrections never shown) are fixed. The example
contract renders to 240 words.

## Hermes: inline instructions, its own PATH, run limits

The wrapper puts the text of `IDENTITY.md` in the prompt. It starts Hermes with the venv's
own Python by path (with `-P`, since the working directory is the task's checkout and a
`tools` or `cli` module there would otherwise be imported in place of Hermes's own) and
sets no PATH, so the model's `python3` and `uv` are the image's. Hermes 0.19 prepends the
directory of the `hermes` it finds to every command's PATH, falling back to its own venv;
the image links `/usr/local/bin/hermes` so what it finds is already on PATH.
Hermes 0.19's `-z` builds its agent with a fixed 90-turn budget and reads no setting for
it; the wrapper runs Hermes's own entry point under a bootstrap that sets the budget when
the caller named none (checked against 0.19.0 in the image: 300 applied, an explicit 45
kept). The context window is Hermes's own `model.context_length`, written to its home.
Defaults: 300 turns (the operator's own Hermes uses 150 interactively; a task that reads,
edits and runs checks needs more) and 131072 tokens (above Hermes's 64000 floor; not read
from the gateway and not checked against the lab's models, so a model with a smaller
window needs its real figure, or 0 to let Hermes probe for it). Both are on the Local gateway page, the admin API
and `crucible-admin gateway limits`. A run that ends on its turn budget is recorded as
`harness_limit_reached` evidence, not failed.

## Package caches

`UV_CACHE_DIR`, `PIP_CACHE_DIR` and `npm_config_cache` point at a leaf of the workspace
instead of the 512Mi memory home. The worker and the verifier each have their own: uv and
pip reuse what is in their cache without checking it again, so sharing the worker's cache
would let the worker change what the verifier's checks run.
