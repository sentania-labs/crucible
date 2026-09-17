"""The shell the collector, the bundle verifier, and the verifier run (08, 11).

These are Crucible's own scripts, not the worker's. They run inside throwaway
containers built from the same hardened shape as a worker, so nothing here needs to
trust the tree it is reading: everything read out of the workspace is data.

Git in the collector runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
and the `-c` overrides 08 names, so a `.git/config` or a committed hook cannot make it
run anything.
"""

from __future__ import annotations

from crucible.ports.execution import (
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
)

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
  {GIT} clone --mirror {_quote(url)} "{cache_dir}" || true
fi
if [ -d "{cache_dir}" ]; then
  REFERENCE="--reference {cache_dir} --dissociate"
fi
"""
        if cache_name
        else ""
    )
    resume = "1" if from_remote_branch else "0"
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
OUT={WORK_MOUNT}/output
REPO={WORK_MOUNT}/repo
mkdir -p "$OUT"
rm -rf "$REPO"
REFERENCE=""
{refresh}
# shellcheck disable=SC2086
{GIT} clone --no-hardlinks --no-checkout $REFERENCE {_quote(url)} "$REPO"
cd "$REPO"
STARTED=""
REMOTE_BRANCH="refs/remotes/origin/{work_branch}"
if [ "{resume}" = "1" ] && {GIT} rev-parse --verify --quiet "$REMOTE_BRANCH" >/dev/null; then
  {GIT} checkout -B "{work_branch}" "origin/{work_branch}"
  STARTED="origin/{work_branch}"
else
  if {GIT} rev-parse --verify --quiet "refs/remotes/origin/{base_ref}" >/dev/null; then
    TARGET="refs/remotes/origin/{base_ref}"
  elif {GIT} rev-parse --verify --quiet "{base_ref}" >/dev/null; then
    TARGET="{base_ref}"
  else
    echo "base ref {base_ref} does not exist in the clone" >&2
    exit 3
  fi
  {GIT} checkout -B "{work_branch}" "$TARGET"
  STARTED="{base_ref}"
fi
{GIT} remote set-url origin {_quote(origin_placeholder)}
{GIT} remote set-url --push origin {_quote(origin_placeholder)}
{GIT} config user.name {_quote(author_name)}
{GIT} config user.email {_quote(author_email)}
{GIT} config credential.helper ""
{GIT} config http.extraHeader ""

# 06: a shim only where the checkout has none, and every shim listed in
# .git/info/exclude so its absence from the diff stays a gate (11). The container
# writes them because the checkout belongs to container uid 1000, which is not the
# uid the Crucible process runs as in every arrangement (S9 Test E).
mkdir -p "$REPO/.git/info"
SHIM_TEXT="Read {identity_mount}/IDENTITY.md first; it is the task contract for this run."
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
mkdir -p "$OUT"
: > "$OUT/copy-rejections.tsv"
{_COPY_REPORT}
BASE=$({GIT} -C "$REPO" rev-parse --verify --quiet {base_ref} \
  || {GIT} -C "$REPO" rev-parse --verify --quiet origin/{base_ref} \
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
    "$BASE".."{work_branch}" > "$OUT/bundle.log" 2>&1 || true
  {GIT} -C "$REPO" rev-list --count "$BASE"..HEAD > "$OUT/commits.txt" \
    || echo 0 > "$OUT/commits.txt"
else
  : > "$OUT/diffstat.txt"; : > "$OUT/diff.patch"; : > "$OUT/changed.txt"
  : > "$OUT/log.txt"; : > "$OUT/commit-paths.txt"; echo 0 > "$OUT/commits.txt"
fi
# A fresh tree from the collected state, which is what the verifier runs against (11).
rm -rf "$OUT/tree"
{GIT} clone --no-hardlinks --quiet "$REPO" "$OUT/tree" > "$OUT/clone.log" 2>&1 || true
copy_report "{REPORT_MOUNT}" "$OUT/report" {size_cap_bytes}
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


def verifier_script(checks: list[tuple[str, str]]) -> str:
    """Re-run each `required_verification` command from the collected tree (11).

    Each command's exit and log go to the verify directory, which is the only place
    this container may write besides its own tree copy. The commands come from the
    repository, so this container is the one that runs worker-influenced code: it
    never sees the collector's output directory, only its own tree."""
    lines = [
        "set -u",
        f"cd {REPO_MOUNT}",
        "export HOME=/home/worker LC_ALL=C",
        f"mkdir -p {VERIFY_MOUNT}",
    ]
    for check_id, command in checks:
        safe = check_id.replace("/", "_")
        lines.append(f"printf '%s\\n' {_quote(command)} > {VERIFY_MOUNT}/{safe}.cmd")
        lines.append(
            f"sh -c {_quote(command)} > {VERIFY_MOUNT}/{safe}.log 2>&1; "
            f"echo $? > {VERIFY_MOUNT}/{safe}.exit"
        )
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
