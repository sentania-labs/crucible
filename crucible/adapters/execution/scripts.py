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
)

GIT = (
    "git -c core.fsmonitor= -c diff.external= -c core.pager=cat "
    "-c core.hooksPath=/dev/null -c safe.directory=*"
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
