#!/usr/bin/env bash
# Launch the local live-trading dashboard.
#
# Reads web/config.yaml (copy from web/config.example.yaml first) plus any .env at
# the repo root. Bound to 127.0.0.1 by default — this tool has no auth, do not
# expose it on a public interface.
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${DASHBOARD_HOST:-0.0.0.0}"
PORT="${DASHBOARD_PORT:-6040}"

exec uvicorn web.app:app --host "$HOST" --port "$PORT" "$@"
