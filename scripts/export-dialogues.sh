#!/usr/bin/env bash
# Public auto/collect/update/merge entrypoint; no offline conversion mode.
set -eu
case "${1:-}" in
  auto|collect|update|merge) ;;
  *) set -- auto "$@" ;;
esac
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/chats.py" "$@"
