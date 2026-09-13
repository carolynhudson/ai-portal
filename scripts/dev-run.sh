#!/usr/bin/env bash
# Run the portal from the SOURCE tree on :8091 with debug logging, alongside
# the installed service on :8090. Use it to try a change before deploying.
#
#   ./scripts/dev-run.sh              # http://127.0.0.1:8091
#   PORT=8123 ./scripts/dev-run.sh    # another port
#   LOG=trace ./scripts/dev-run.sh    # very chatty probes
#
# IMPORTANT — AI_PORTAL_HOME is pinned to the real home on purpose. Agent and
# sandbox shells can export HOME to a scratch directory, and every ~/ai path
# derives from it: the dev instance then silently "manages" $SANDBOX/ai (an
# empty tree) instead of the real stack. That happened on 2026-09-11 and looked
# exactly like the portal losing track of the router. Override deliberately with
# REAL_HOME=/some/where ./scripts/dev-run.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$here")"

export AI_PORTAL_HOME="${REAL_HOME:-$HOME}"
export AI_PORTAL_HOST="${HOST:-127.0.0.1}"
export AI_PORTAL_PORT="${PORT:-8091}"
export AI_PORTAL_LOG_LEVEL="${LOG:-debug}"

if [ "$AI_PORTAL_HOME" != "$(getent passwd "$(id -u)" | cut -d: -f6)" ]; then
  echo "NOTE: AI_PORTAL_HOME=$AI_PORTAL_HOME is not your passwd home —" >&2
  echo "      the portal will manage that tree, not ~/ai." >&2
fi

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "no .venv — creating it (uv sync)" >&2
  uv sync
fi

echo "ai-portal dev: http://${AI_PORTAL_HOST}:${AI_PORTAL_PORT}  home=${AI_PORTAL_HOME}  log=${AI_PORTAL_LOG_LEVEL}"
exec "$PY" -m uvicorn app.main:app \
  --host "$AI_PORTAL_HOST" --port "$AI_PORTAL_PORT" \
  --log-level "${UVICORN_LOG:-info}" --reload
