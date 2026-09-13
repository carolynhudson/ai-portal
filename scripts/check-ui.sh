#!/usr/bin/env bash
# Run the portal's test suite, UI integration tests included.
#
#   ./scripts/check-ui.sh              # everything (use this after every change)
#   ./scripts/check-ui.sh --ui-only    # UI suites only (fastest feedback)
#   ./scripts/check-ui.sh -k dashboard # pass through to pytest
#
# Why this exists as a single command: the portal's UI has no build step, so a
# template edit that breaks a <script> block produces no error anywhere until a
# user clicks a dead button. scripts/check-ui.sh is what turns "I edited a
# template" into "the pages still work, every button still does something".
#
# The UI tests drive a real browser-grade DOM (jsdom) against a sandboxed
# portal: temp AI_PORTAL_HOME, shimmed systemctl/journalctl, dead outbound
# proxy, and service/update mutations intercepted in the harness. Running them
# cannot restart, stop, rebuild or reconfigure anything real.
set -uo pipefail

cd "$(dirname "$0")/.."
UI_DIR="tests/ui"

# --- node + jsdom ----------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node is required for the UI tests (they drive the real page JS)." >&2
  echo "       install node, or run the non-UI suite with:" >&2
  echo "       uv run --with pytest python -m pytest tests/ -q --ignore=tests/test_ui_integration.py" >&2
  exit 127
fi

if [ ! -d "$UI_DIR/node_modules/jsdom" ]; then
  echo "installing jsdom into $UI_DIR (one time, ~10s)…"
  (cd "$UI_DIR" && npm install --silent --no-fund --no-audit) || {
    echo "ERROR: npm install failed in $UI_DIR" >&2
    exit 127
  }
fi

# --- pytest ----------------------------------------------------------------
PYTEST=(uv run --with pytest python -m pytest)
if [ "${1:-}" = "--ui-only" ]; then
  shift
  exec "${PYTEST[@]}" tests/test_ui_contract.py tests/test_ui_integration.py "$@"
fi
exec "${PYTEST[@]}" tests/ "$@"
