#!/usr/bin/env bash
# Install + enable the ai-portal systemd *user* service (decision D2: LAN + password).
#
#   ./deploy/install-service.sh          # install + start now
#   ./deploy/install-service.sh --no-start
#
# First run of the portal generates a password and prints it once to stderr and
# to ~/ai/state/portal/PORTAL_PASSWORD.txt (0600). Set AI_PORTAL_PASSWORD in the
# unit's Environment= to pin your own instead.
set -euo pipefail
cd "$(dirname "$0")/.."

# make sure the venv + deps exist
if [ ! -x .venv/bin/python ]; then
  echo "no venv — running: uv sync" >&2
  uv sync
fi

UNIT="$HOME/.config/systemd/user/ai-portal.service"
mkdir -p "$(dirname "$UNIT")"
cp deploy/ai-portal.service "$UNIT"
systemctl --user daemon-reload

if [ "${1:-}" = "--no-start" ]; then
  systemctl --user enable ai-portal.service
  echo "ai-portal enabled (not started). Start with: systemctl --user start ai-portal"
else
  systemctl --user enable --now ai-portal.service
fi

echo
systemctl --user --no-pager status ai-portal.service | head -8
echo
echo "Open  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8090   (or http://localhost:8090)"
echo "Password (first run): $(cat ~/ai/state/portal/PORTAL_PASSWORD.txt 2>/dev/null || echo 'see portal log')"
