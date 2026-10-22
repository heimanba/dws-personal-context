#!/usr/bin/env bash
set -eu
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/context.py" update "$@"
