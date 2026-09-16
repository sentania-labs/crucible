#!/usr/bin/env bash
# git credential helper for spike S10. Reads the installation token from the
# tmpfs file and answers git's "get" request, but only for github.com over
# https. git writes the request (protocol, host, path) on stdin; a helper that
# answers for any host hands the token to whatever remote git was pointed at,
# so the host check is part of the design, not a spike detail.
set -u
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
[ "$host" = "github.com" ] || exit 0

printf 'username=x-access-token\n'
printf 'password=%s\n' "$(< /run/crucible-token/token)"
