#!/usr/bin/env bash
# Install the repository's git hooks.
#
#   ./scripts/install-hooks.sh
#
# pre-commit runs the UI test suites that apply to what is staged: the fast
# static contract checks for any app/template change, and the full DOM sweep
# when a template, the harness or the app itself changes. That is what keeps
# "I only edited a template" from shipping a page whose JavaScript no longer
# parses. Escape hatch: `git commit --no-verify` (say why in the message).
set -euo pipefail
cd "$(dirname "$0")/.."
HOOKS_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS_DIR"
cp scripts/hooks/pre-commit "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"
echo "installed $HOOKS_DIR/pre-commit"
