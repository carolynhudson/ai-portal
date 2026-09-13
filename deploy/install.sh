#!/usr/bin/env bash
# Deploy / update the AI Portal as a systemd *user* service running from ~/ai.
#
#   ./deploy/install.sh                  # deploy current HEAD -> ~/ai/ai-portal
#   ./deploy/install.sh --dry-run        # show every action, change nothing
#   ./deploy/install.sh --ref v0.2.0     # deploy a specific commit or tag
#   ./deploy/install.sh --no-start       # install + enable, start later
#   ./deploy/install.sh --status         # recorded deployment + unit state
#   ./deploy/install.sh --rollback       # redeploy the previous recorded sha
#
# Thin wrapper around `python -m app.deploy`, which is stdlib-only on purpose:
# it has to work from a fresh clone before any venv exists (it creates the
# venv in the *target*, ~/ai/ai-portal/.venv).
#
# Layout it produces (repo stays the place code is written, ~/ai is where it
# runs — see docs/PORTAL_PLAN.md §0):
#
#   ~/ai/ai-portal/            exported git ref + .venv
#   ~/ai/config/portal.env     launch parameters (bind, port, log level)
#   ~/ai/bin/ai-portal.sh      generated wrapper the unit execs
#   ~/.config/systemd/user/ai-portal.service
#   ~/ai/state/portal/deployment.json
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$here")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found (set PYTHON=...)" >&2; exit 1; }

"$PY" -m app.deploy "$@"

# operator hints only after a real deploy
case "$*" in
  *--dry-run*|*--status*|*--print-units*) exit 0 ;;
esac

echo
lan="$(python3 - <<'EOF' 2>/dev/null || true
import ipaddress, re, subprocess
try:
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                         text=True, timeout=5).stdout
    route = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                           text=True, timeout=5).stdout
    dev = re.search(r"dev\s+(\S+)", route)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and (not dev or parts[1] == dev.group(1)):
            print(parts[3].split("/")[0]); break
except Exception:
    pass
EOF
)"
echo "Portal:      http://localhost:8090/    (this machine)"
[ -n "$lan" ] && echo "             http://${lan}:8090/    (local network, if bound to 0.0.0.0)"
pwfile="$HOME/ai/state/portal/PORTAL_PASSWORD.txt"
[ -f "$pwfile" ] && echo "First-run password: ${pwfile} (delete after reading)"
echo "Logs:        journalctl --user -u ai-portal -f     and   ~/ai/logs/portal.log"
