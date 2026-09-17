"""The shell the collector, the bundle verifier, and the verifier run (08, 11).

These are Crucible's own scripts, not the worker's. They run inside throwaway
containers built from the same hardened shape as a worker, so nothing here needs to
trust the tree it is reading: everything read out of the workspace is data.

Git in the collector runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
and the `-c` overrides 08 names, so a `.git/config` or a committed hook cannot make it
run anything.

Nothing a contract carries is ever pasted into a command line as text. Every such value
is bound to a shell variable from a single-quoted literal and referenced quoted, so a
ref, a path or a check id is data to `sh` whatever it contains. The contract refuses a
ref outside `[A-Za-z0-9._/-]` on top of that (05): quoting is what stops it executing,
validation is what stops it being an option.
"""

from __future__ import annotations

from crucible.ports.execution import (
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
)

__all__ = [
    "BUNDLE_MOUNT",
    "BUNDLE_VERIFY_SCRIPT",
    "MANIFEST",
    "REPO_MOUNT",
    "VERIFY_MOUNT",
    "collector_script",
    "encode_check_id",
    "preparer_script",
    "publisher_script",
    "verifier_script",
]

CACHE_MOUNT = "/crucible/cache"
ORIGIN_MOUNT = "/crucible/origin"

GIT = (
    "git -c core.fsmonitor= -c diff.external= -c core.pager=cat "
    "-c core.hooksPath=/dev/null -c 'safe.directory=*'"
)
GIT_ENV = (
    "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "
    "GIT_TERMINAL_PROMPT=0 GIT_ASKPASS= HOME=/home/worker LC_ALL=C"
)

# 08: the copy step rejects symlinks, hard links, devices, and files above the policy
# size cap, and records each rejection. `find -P` (the default) never descends through
# a symlink, so a path whose component is a symlink never reaches the copy at all.
_COPY_REPORT = r"""
copy_report() {
  src="$1"; dst="$2"; cap="$3"
  mkdir -p "$dst"
  find -P "$src" -mindepth 1 | LC_ALL=C sort | while IFS= read -r p; do
    rel=${p#"$src"/}
    if [ -h "$p" ]; then
      printf 'symlink\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    if [ -d "$p" ]; then mkdir -p "$dst/$rel"; continue; fi
    if [ ! -f "$p" ]; then
      printf 'not-a-regular-file\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    links=$(find -P "$p" -prune -printf '%n' 2>/dev/null || echo 1)
    if [ "${links:-1}" -gt 1 ]; then
      printf 'hard-link\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    size=$(wc -c < "$p")
    if [ "$size" -gt "$cap" ]; then
      printf 'over-size-cap\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    mkdir -p "$dst/$(dirname "$rel")"
    cat < "$p" > "$dst/$rel"
  done
}
"""


def preparer_script(
    *,
    url: str,
    base_ref: str,
    work_branch: str,
    from_remote_branch: bool,
    cache_name: str | None,
    author_name: str,
    author_email: str,
    origin_placeholder: str,
    shims: tuple[str, ...],
    exclude_entries: tuple[str, ...],
    identity_mount: str,
) -> str:
    """Clone, position, and seal the checkout (08).

    The reference cache is a bare mirror this script refreshes and clones from with
    `--dissociate`, so the checkout owns its objects and nothing shared is ever
    mounted into a worker. The origin URL is replaced with a placeholder before the
    worker sees it, and no credential helper is configured, so a push cannot start.
    """
    cache_dir = f"{CACHE_MOUNT}/{cache_name}.git" if cache_name else ""
    refresh = (
        f"""
if [ -d "{cache_dir}" ]; then
  {GIT} --git-dir "{cache_dir}" fetch --prune origin || rm -rf "{cache_dir}"
fi
if [ ! -d "{cache_dir}" ]; then
  {GIT} clone --mirror -- "$CLONE_URL" "{cache_dir}" || true
fi
if [ -d "{cache_dir}" ]; then
  REFERENCE="--reference {cache_dir} --dissociate"
fi
"""
        if cache_name
        else ""
    )
    resume = "1" if from_remote_branch else "0"
    # Bound as literals, referenced quoted, and never concatenated into a command.
    bindings = "\n".join(
        (
            f"WORK_BRANCH={_quote(work_branch)}",
            f"BASE_REF={_quote(base_ref)}",
            f"CLONE_URL={_quote(url)}",
            f"ORIGIN_PLACEHOLDER={_quote(origin_placeholder)}",
            f"AUTHOR_NAME={_quote(author_name)}",
            f"AUTHOR_EMAIL={_quote(author_email)}",
            f"IDENTITY_MOUNT={_quote(identity_mount)}",
        )
    )
    shim_list = " ".join(_quote(name) for name in shims)
    exclude_block = "\n".join(
        f'grep -qxF {_quote(entry)} "$REPO/.git/info/exclude" '
        f"|| printf '%s\\n' {_quote(entry)} >> \"$REPO/.git/info/exclude\""
        for entry in exclude_entries
    )
    return f"""set -eu
{GIT_ENV}
# git ignores `safe.directory` from the command line, and the directories this
# container reads belong to whichever uid the host gave them, which is not the uid the
# daemon runs this container as (S9 Test E). The exception therefore goes in a global
# config file, written here, in this container's own tmpfs, from Crucible's own text.
# Only the preparer gets it: the collector keeps GIT_CONFIG_GLOBAL=/dev/null, because
# what it reads is a tree a worker wrote.
printf '[safe]\n\tdirectory = *\n' > /tmp/gitconfig
export GIT_CONFIG_GLOBAL=/tmp/gitconfig
{bindings}
OUT={WORK_MOUNT}/output
REPO={WORK_MOUNT}/repo
mkdir -p "$OUT"
rm -rf "$REPO"
REFERENCE=""
{refresh}
# shellcheck disable=SC2086
{GIT} clone --no-hardlinks --no-checkout $REFERENCE -- "$CLONE_URL" "$REPO"
cd "$REPO"
STARTED=""
if [ "{resume}" = "1" ] \
  && {GIT} rev-parse --verify --quiet "refs/remotes/origin/$WORK_BRANCH" >/dev/null; then
  {GIT} checkout -B "$WORK_BRANCH" "origin/$WORK_BRANCH" --
  STARTED="origin/$WORK_BRANCH"
else
  if {GIT} rev-parse --verify --quiet "refs/remotes/origin/$BASE_REF" >/dev/null; then
    TARGET="refs/remotes/origin/$BASE_REF"
  elif {GIT} rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
    TARGET="$BASE_REF"
  else
    printf 'base ref %s does not exist in the clone\n' "$BASE_REF" >&2
    exit 3
  fi
  {GIT} checkout -B "$WORK_BRANCH" "$TARGET" --
  STARTED="$BASE_REF"
fi
{GIT} remote set-url origin "$ORIGIN_PLACEHOLDER"
{GIT} remote set-url --push origin "$ORIGIN_PLACEHOLDER"
{GIT} config user.name "$AUTHOR_NAME"
{GIT} config user.email "$AUTHOR_EMAIL"
{GIT} config credential.helper ""
{GIT} config http.extraHeader ""

# 06: a shim only where the checkout has none, and every shim listed in
# .git/info/exclude so its absence from the diff stays a gate (11). The container
# writes them because the checkout belongs to container uid 1000, which is not the
# uid the Crucible process runs as in every arrangement (S9 Test E).
mkdir -p "$REPO/.git/info"
SHIM_TEXT="Read $IDENTITY_MOUNT/IDENTITY.md first; it is the task contract for this run."
for shim in {shim_list}; do
  if [ ! -e "$REPO/$shim" ]; then
    printf '%s\\n' "$SHIM_TEXT" > "$REPO/$shim"
  fi
done
touch "$REPO/.git/info/exclude"
{exclude_block}

mkdir -p "$OUT"
{GIT} rev-parse HEAD > "$OUT/prepared-head.txt"
printf '%s\n' "$STARTED" > "$OUT/started-from.txt"
"""


def collector_script(*, base_ref: str, work_branch: str, size_cap_bytes: int) -> str:
    """Produce the full diff, the path list, the head, the log, the bundle, and a copy
    of the report directory (08). Never a push, never a network: `--network none`."""
    return f"""set -eu
{GIT_ENV}
OUT={OUTPUT_MOUNT}
REPO={REPO_MOUNT}
WORK_BRANCH={_quote(work_branch)}
BASE_REF={_quote(base_ref)}
SIZE_CAP={_quote(str(size_cap_bytes))}
mkdir -p "$OUT"
: > "$OUT/copy-rejections.tsv"
{_COPY_REPORT}
BASE=$({GIT} -C "$REPO" rev-parse --verify --quiet "$BASE_REF" \
  || {GIT} -C "$REPO" rev-parse --verify --quiet "origin/$BASE_REF" \
  || echo "")
printf '%s\\n' "$BASE" > "$OUT/base.txt"
{GIT} -C "$REPO" rev-parse HEAD > "$OUT/head.txt"
{GIT} -C "$REPO" rev-parse --abbrev-ref HEAD > "$OUT/branch.txt"
if [ -n "$BASE" ]; then
  {GIT} -C "$REPO" diff --stat "$BASE"..HEAD > "$OUT/diffstat.txt" || true
  {GIT} -C "$REPO" diff --no-color --no-ext-diff "$BASE"..HEAD > "$OUT/diff.patch" || true
  {GIT} -C "$REPO" diff --name-only "$BASE"..HEAD > "$OUT/changed.txt" || true
  {GIT} -C "$REPO" log --format='%H%x1f%s%x1f%an%x1e' "$BASE"..HEAD > "$OUT/log.txt" || true
  {GIT} -C "$REPO" log --name-only --format='' "$BASE"..HEAD \
    | LC_ALL=C sort -u | sed '/^$/d' > "$OUT/commit-paths.txt" || true
  {GIT} -C "$REPO" bundle create "$OUT/work_branch.bundle" \
    "$BASE..$WORK_BRANCH" > "$OUT/bundle.log" 2>&1 || true
  {GIT} -C "$REPO" rev-list --count "$BASE"..HEAD > "$OUT/commits.txt" \
    || echo 0 > "$OUT/commits.txt"
else
  : > "$OUT/diffstat.txt"; : > "$OUT/diff.patch"; : > "$OUT/changed.txt"
  : > "$OUT/log.txt"; : > "$OUT/commit-paths.txt"; echo 0 > "$OUT/commits.txt"
fi
# A fresh tree from the collected state, which is what the verifier runs against (11).
rm -rf "$OUT/tree"
{GIT} clone --no-hardlinks --quiet "$REPO" "$OUT/tree" > "$OUT/clone.log" 2>&1 || true
copy_report "{REPORT_MOUNT}" "$OUT/report" "$SIZE_CAP"
echo done > "$OUT/collector.ok"
"""


BUNDLE_VERIFY_SCRIPT = f"""set -eu
{GIT_ENV}
if [ ! -s {OUTPUT_MOUNT}/work_branch.bundle ]; then
  echo "no bundle was produced" >&2
  exit 2
fi
# `git bundle verify` checks the bundle's prerequisites against a repository, so it
# runs from the fresh tree the collector made, which holds base_ref. The mount is
# read-only and this container runs no command the repository defines.
cd {OUTPUT_MOUNT}/tree
{GIT} bundle verify {OUTPUT_MOUNT}/work_branch.bundle
"""


# The characters a verification id may keep in a file name. Everything else is
# percent-encoded, so two ids that differ only outside this set still get two files:
# `a/b` becomes `a%2Fb` and `a_b` stays `a_b`, which a plain substitution would have
# collapsed into one file and one exit code.
_FILENAME_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
MANIFEST = "ids.tsv"


def encode_check_id(check_id: str) -> str:
    """Percent-encode a verification id into a file name, reversibly and injectively."""
    out: list[str] = []
    for char in check_id:
        if char in _FILENAME_SAFE and not (char == "." and not out):
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out) or "%00"


def verifier_script(checks: list[tuple[str, str]]) -> str:
    """Re-run each `required_verification` command from the collected tree (11).

    Each command's exit and log go to the verify directory, which is the only place
    this container may write besides its own tree copy. The commands come from the
    repository, so this container is the one that runs worker-influenced code: it
    never sees the collector's output directory, only its own tree.

    The command is executed as the contract gave it, which is the point of the gate.
    Everything else, the id and the file names it becomes, is bound as a shell variable
    from a literal and never concatenated into a command."""
    lines = [
        "set -u",
        f"cd {REPO_MOUNT}",
        "export HOME=/home/worker LC_ALL=C",
        f"V={_quote(VERIFY_MOUNT)}",
        'mkdir -p "$V"',
        f'MANIFEST="$V/{MANIFEST}"',
        ': > "$MANIFEST"',
    ]
    for check_id, command in checks:
        encoded = encode_check_id(check_id)
        lines.append(f"ID={_quote(check_id)}")
        lines.append(f"F={_quote(encoded)}")
        lines.append(f"CMD={_quote(command)}")
        lines.append('printf \'%s\\t%s\\n\' "$F" "$ID" >> "$MANIFEST"')
        lines.append('printf \'%s\\n\' "$CMD" > "$V/$F.cmd"')
        lines.append('sh -c "$CMD" > "$V/$F.log" 2>&1; echo $? > "$V/$F.exit"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


# ----- the publisher (23, S10) -------------------------------------------

BUNDLE_MOUNT = "/crucible/bundle"
TOKEN_MOUNT = "/run/crucible-token"
PUBLISH_MOUNT = "/crucible/publish"

# git's credential helper protocol: git writes `protocol=`, `host=` and friends on
# stdin and reads `username=` and `password=` back. The helper answers only for
# https on the one configured host, because a helper that answers unconditionally hands
# the token to whatever remote git was pointed at (S10). `store` and `erase` are ignored.
#
# The helper is run through `sh` rather than executed: a Docker tmpfs is mounted
# `noexec` unless `exec` is asked for, and the publisher's `/tmp` keeps `noexec`. git
# runs a helper value beginning with `!` through the shell, which is what this uses. The
# value has spaces, so it lives in a git config file in the container's own tmpfs rather
# than on a command line, which is the same shape the preparer uses (C3).
_CRED_HELPER = r"""
cat > /tmp/cred-helper.sh <<'HELPER'
#!/bin/sh
[ "${1:-}" = "get" ] || exit 0
protocol= host=
while IFS='=' read -r key value; do
  [ -z "$key" ] && break
  case "$key" in
    protocol) protocol=$value ;;
    host) host=$value ;;
  esac
done
[ "$protocol" = "https" ] || exit 0
[ "$host" = "$CRUCIBLE_CREDENTIAL_HOST" ] || exit 0
printf 'username=x-access-token\n'
printf 'password=%s\n' "$(cat "$CRUCIBLE_TOKEN_FILE")"
HELPER
chmod 0600 /tmp/cred-helper.sh
{
  printf '[credential]\n\thelper = "!sh /tmp/cred-helper.sh"\n'
  printf '[core]\n\thooksPath = /dev/null\n\tpager = cat\n'
  printf '[user]\n\tname = %s\n\temail = %s\n' \
    "$CRUCIBLE_AUTHOR_NAME" "$CRUCIBLE_AUTHOR_EMAIL"
} > /tmp/gitconfig
chmod 0600 /tmp/gitconfig
export GIT_CONFIG_GLOBAL=/tmp/gitconfig
"""


def publisher_script(
    *,
    clone_url: str,
    work_branch: str,
    base_ref: str,
    expected_head: str,
    author_name: str,
    author_email: str,
    commit_trailer: str,
    credential_host: str = "github.com",
) -> str:
    """Fetch the base from the remote and the branch from the bundle, then push (23).

    The bundle is `base_ref..work_branch`, so it names prerequisite commits and neither
    `git bundle verify` nor `git fetch` will look at it until the repository has them.
    The publisher fetches `base_ref` from the real remote first, which is the only tree
    it ever sees: it never touches the worker's checkout or the worker's `.git`, and the
    bundle is the only carrier of the worker's commits.

    The token arrives on stdin and is written to a tmpfs file before anything else
    happens; `docker cp` cannot reach a tmpfs inside a read-only container and even
    without `--read-only` it targets the writable layer, which is disk (S10). Nothing
    here ever carries the value on argv: the credential helper reads the file, and no
    curl runs at all, because the API calls stay on Crucible's side.

    `GIT_TRACE*` and `GIT_CURL_VERBOSE` print the Authorization header, so the script
    unsets them rather than trusting the environment it inherited (S10 risks)."""
    return f"""set -eu
umask 077
TOKDIR={_quote(TOKEN_MOUNT)}
OUT={_quote(PUBLISH_MOUNT)}
BUNDLE={_quote(BUNDLE_MOUNT)}/work_branch.bundle
WORK_BRANCH={_quote(work_branch)}
BASE_REF={_quote(base_ref)}
EXPECTED={_quote(expected_head)}
CLONE_URL={_quote(clone_url)}
TRAILER={_quote(commit_trailer)}
mkdir -p "$OUT"
cat > "$TOKDIR/token"
if [ ! -s "$TOKDIR/token" ]; then
  echo "no token arrived on stdin" > "$OUT/error.txt"; echo no-token > "$OUT/step.txt"; exit 3
fi
chmod 0600 "$TOKDIR/token"
# Back to the ordinary mask before anything is written to the output directory: what
# lands there is Crucible's own record of the run, and under the rootless daemon this
# container's uid is not the one that reads it back (S9 Test E).
umask 022
stat -c '%a' "$TOKDIR/token" > "$OUT/token-mode.txt"
unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_PACKET GIT_TRACE2 || true
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME=/home/worker LC_ALL=C
export CRUCIBLE_TOKEN_FILE="$TOKDIR/token"
export CRUCIBLE_CREDENTIAL_HOST={_quote(credential_host)}
export CRUCIBLE_AUTHOR_NAME={_quote(author_name)}
export CRUCIBLE_AUTHOR_EMAIL={_quote(author_email)}
{_CRED_HELPER}
cd /home/worker
rm -rf publish && mkdir publish && cd publish
echo init > "$OUT/step.txt"
git init --quiet -b "$BASE_REF" >> "$OUT/publisher.log" 2>&1
git remote add origin "$CLONE_URL"
echo fetch-base > "$OUT/step.txt"
git fetch --quiet origin "refs/heads/$BASE_REF:refs/remotes/origin/$BASE_REF" \
  >> "$OUT/publisher.log" 2>&1
echo bundle-verify > "$OUT/step.txt"
git bundle verify "$BUNDLE" >> "$OUT/publisher.log" 2>&1
echo fetch-bundle > "$OUT/step.txt"
git fetch --quiet "$BUNDLE" "refs/heads/$WORK_BRANCH:refs/heads/crucible-publish" \
  >> "$OUT/publisher.log" 2>&1
HEAD_SHA=$(git rev-parse refs/heads/crucible-publish)
printf '%s\n' "$HEAD_SHA" > "$OUT/bundle-head.txt"
if [ "$HEAD_SHA" != "$EXPECTED" ]; then
  echo "the bundle head $HEAD_SHA is not the collected head $EXPECTED" > "$OUT/error.txt"
  echo head-mismatch > "$OUT/step.txt"; exit 4
fi
echo ls-remote > "$OUT/step.txt"
git ls-remote origin "refs/heads/$WORK_BRANCH" > "$OUT/ls-remote-before.txt" \
  2>> "$OUT/publisher.log" || true
awk '{{print $1}}' "$OUT/ls-remote-before.txt" | head -n 1 > "$OUT/remote-head-before.txt"
# Every commit the bundle carries must match the policy's author and the attempt
# trailer, checked here where the commits are, not after they are on the remote.
echo commit-policy > "$OUT/step.txt"
REMOTE_BEFORE=$(cat "$OUT/remote-head-before.txt")
if [ -n "$REMOTE_BEFORE" ]; then
  git fetch --quiet origin "refs/heads/$WORK_BRANCH:refs/remotes/origin/$WORK_BRANCH" \
    >> "$OUT/publisher.log" 2>&1 || true
  RANGE="refs/remotes/origin/$WORK_BRANCH..refs/heads/crucible-publish"
else
  RANGE="refs/remotes/origin/$BASE_REF..refs/heads/crucible-publish"
fi
: > "$OUT/author-problems.txt"
: > "$OUT/trailer-problems.txt"
for sha in $(git rev-list "$RANGE" 2>/dev/null || true); do
  who=$(git show -s --format='%ae' "$sha")
  if [ "$who" != {_quote(author_email)} ]; then
    printf '%s\t%s\n' "$sha" "$who" >> "$OUT/author-problems.txt"
  fi
  if ! git show -s --format='%(trailers:key='"$TRAILER"',valueonly)' "$sha" | grep -q .; then
    printf '%s\n' "$sha" >> "$OUT/trailer-problems.txt"
  fi
done
echo push > "$OUT/step.txt"
# No force, ever. A remote head that is not an ancestor of the bundle head fails here,
# which is exactly what 23 asks for: record it, wake Foundry, never overwrite.
if git push --quiet origin "refs/heads/crucible-publish:refs/heads/$WORK_BRANCH" \
    2> "$OUT/push.err"; then
  echo ok > "$OUT/push.txt"
else
  echo failed > "$OUT/push.txt"
  cp "$OUT/push.err" "$OUT/error.txt" 2>/dev/null || true
  echo push > "$OUT/step.txt"
  rm -f "$TOKDIR/token"
  exit 5
fi
echo done > "$OUT/step.txt"
rm -f "$TOKDIR/token"
chmod 0644 "$OUT"/* 2>/dev/null || true
exit 0
"""
