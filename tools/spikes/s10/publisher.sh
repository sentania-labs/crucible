#!/usr/bin/env bash
# Spike S10 publisher: runs inside the hardened container as uid 1000.
# Waits for the token to land on tmpfs, clones the target repository, pushes
# one branch and one annotated tag, and opens a PR through the REST API.
# Nothing here prints the token; curl reads it from a config file on tmpfs,
# git through the credential helper.
set -euo pipefail

: "${S10_REPO:?owner/name}" "${S10_BRANCH:?}" "${S10_TAG:?}" "${S10_STAMP:?}"
: "${S10_BASE:=main}" "${S10_MODE:=full}"
TOKDIR=/run/crucible-token
OUT=/out
LIB=/usr/local/lib/crucible-s10
API=https://api.github.com

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
now_ms() { date +%s%3N; }

# 1. receive the token on stdin (docker run -i; Crucible writes it and closes
#    the pipe). docker cp cannot reach a tmpfs inside a --read-only container,
#    and stdin is neither logged, inspected, nor visible in ps or env.
umask 077
cat > "$TOKDIR/token"
umask 022
[ -s "$TOKDIR/token" ] || { log "no token arrived on stdin"; exit 3; }
log "token present on tmpfs ($(stat -c '%U:%G %a' "$TOKDIR/token"))"

# curl config on tmpfs; printf and $(<file) are bash builtins, so no process
# ever carries the value on argv.
umask 077
printf 'header = "Authorization: Bearer %s"\n' "$(< "$TOKDIR/token")" > "$TOKDIR/curlrc"
printf 'header = "Accept: application/vnd.github+json"\nheader = "X-GitHub-Api-Version: 2022-11-28"\nsilent\nshow-error\n' >> "$TOKDIR/curlrc"
umask 022

api() {  # api <name> <method> <path> [json-body]
    local name=$1 method=$2 path=$3 body=${4:-}
    local t0 t1 status
    t0=$(now_ms)
    if [ -n "$body" ]; then
        status=$(curl -K "$TOKDIR/curlrc" -X "$method" "$API$path" \
            -H 'Content-Type: application/json' --data-binary "$body" \
            -D "$OUT/$name.headers" -o "$OUT/$name.json" -w '%{http_code}')
    else
        status=$(curl -K "$TOKDIR/curlrc" -X "$method" "$API$path" \
            -D "$OUT/$name.headers" -o "$OUT/$name.json" -w '%{http_code}')
    fi
    t1=$(now_ms)
    local accepted
    accepted=$(grep -i '^x-accepted-github-permissions:' "$OUT/$name.headers" | tr -d '\r' | cut -d' ' -f2- || true)
    log "api $name $method $path -> $status in $((t1 - t0)) ms; accepted=[${accepted}]" >&2
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$method" "$path" "$status" "$((t1 - t0))" "$accepted" >> "$OUT/api-calls.tsv"
    echo "$status"
}

export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
GIT=(git -c credential.helper= -c "credential.helper=$LIB/cred-helper.sh" \
     -c user.name="crucible-spike[bot]" \
     -c user.email="4969317+crucible-spike[bot]@users.noreply.github.com")

cd /home/worker
log "clone start"
t0=$(now_ms)
"${GIT[@]}" clone --quiet "https://github.com/$S10_REPO.git" work
t1=$(now_ms); log "clone done in $((t1 - t0)) ms"
cd work

"${GIT[@]}" checkout --quiet -b "$S10_BRANCH"
mkdir -p spikes
printf 'S10 publisher run %s\nbranch %s\ntag %s\nmode %s\n' "$S10_STAMP" "$S10_BRANCH" "$S10_TAG" "$S10_MODE" > "spikes/S10-$S10_STAMP.txt"
"${GIT[@]}" add spikes
"${GIT[@]}" commit --quiet -m "S10: publisher container commit $S10_STAMP"
HEAD_SHA=$("${GIT[@]}" rev-parse HEAD)
log "commit $HEAD_SHA"

log "push branch start"
t0=$(now_ms)
if "${GIT[@]}" push --quiet origin "refs/heads/$S10_BRANCH:refs/heads/$S10_BRANCH" 2>"$OUT/push-branch.err"; then
    BRANCH_PUSH=ok
else
    BRANCH_PUSH=failed
fi
t1=$(now_ms); log "push branch $BRANCH_PUSH in $((t1 - t0)) ms"
BRANCH_PUSH_MS=$((t1 - t0))

"${GIT[@]}" tag -a "$S10_TAG" -m "S10 annotated tag $S10_STAMP (throwaway)"
log "push tag start"
t0=$(now_ms)
if "${GIT[@]}" push --quiet origin "refs/tags/$S10_TAG" 2>"$OUT/push-tag.err"; then
    TAG_PUSH=ok
else
    TAG_PUSH=failed
fi
t1=$(now_ms); log "push tag $TAG_PUSH in $((t1 - t0)) ms"
TAG_PUSH_MS=$((t1 - t0))

PR_STATUS=skipped
if [ "$S10_MODE" = "full" ] || [ "$S10_MODE" = "pr-expect-403" ]; then
    body=$(jq -cn --arg t "S10 spike PR $S10_STAMP" --arg h "$S10_BRANCH" --arg b "$S10_BASE" \
        --arg body "Throwaway PR opened from the S10 publisher container with an installation token scoped to this repository. Branch head $HEAD_SHA. Will be closed and deleted by the spike." \
        '{title:$t, head:$h, base:$b, body:$body}')
    PR_STATUS=$(api create-pr POST "/repos/$S10_REPO/pulls" "$body")
    if [ "$PR_STATUS" = "201" ]; then
        api get-pr GET "/repos/$S10_REPO/pulls/$(jq -r .number "$OUT/create-pr.json")" >/dev/null
    fi
fi

jq -n --arg head "$HEAD_SHA" --arg bp "$BRANCH_PUSH" --arg tp "$TAG_PUSH" \
      --argjson bpms "$BRANCH_PUSH_MS" --argjson tpms "$TAG_PUSH_MS" --arg pr "$PR_STATUS" --arg mode "$S10_MODE" \
      '{head_sha:$head, branch_push:$bp, branch_push_ms:$bpms, tag_push:$tp, tag_push_ms:$tpms, pr_status:$pr, mode:$mode}' \
      > "$OUT/result.json"
log "done: branch=$BRANCH_PUSH tag=$TAG_PUSH pr=$PR_STATUS"

# Remove the token copies before exit; the tmpfs disappears with the container anyway.
rm -f "$TOKDIR/curlrc" "$TOKDIR/token"
[ "$BRANCH_PUSH" = ok ] || exit 4
