#!/usr/bin/env bash
# One-time install of the GPU-stack update gate (polkit model):
#   /usr/local/lib/aiportal/gpu-update-apply   (root-owned enforcer)
#   /usr/share/polkit-1/actions/com.aiportal.gpuupdate.policy
#   /var/log/aiportal-gpu-update.log
#
#   sudo bash tools/gpu_update_gate_install.sh
#
# After this, the portal's "Apply GPU-stack updates" job runs
#   pkexec /usr/local/lib/aiportal/gpu-update-apply <pkgs>
# and the desktop's polkit agent prompts you for your password — the OS is
# the gate; the portal never sees or stores it.
set -euo pipefail

USERHOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
SRC="$USERHOME/ai-projects/ai-portal/deploy/gpu_update"
BIN=/usr/local/lib/aiportal/gpu-update-apply
POL=/usr/share/polkit-1/actions/com.aiportal.gpuupdate.policy

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo bash $0"; exit 1; }
[ -f "$SRC/gpu-update-apply" ] || { echo "missing $SRC/gpu-update-apply"; exit 1; }

install -d /usr/local/lib/aiportal
install -m 0755 -o root -g root "$SRC/gpu-update-apply" "$BIN"
install -m 0644 -o root -g root "$SRC/com.aiportal.gpuupdate.policy" "$POL"
touch /var/log/aiportal-gpu-update.log
chmod 0640 /var/log/aiportal-gpu-update.log

# The policy must parse and register:
pkaction --verbose --action-id com.aiportal.gpuupdate.apply >/dev/null \
  || { echo "polkit did not register the action (pkaction failed)"; exit 1; }

# The enforcer must refuse junk (run as the invoking user, not root):
runas="$(stat -c %u "$USERHOME")"
refused=0
if sudo -u "#$runas" "$BIN" --help 2>/dev/null; then
  echo "FAIL: non-root invocation was not refused"; exit 1
fi
if "$BIN" bash 2>/dev/null; then  # root here: must be refused by whitelist
  echo "FAIL: whitelist accepted 'bash'"; exit 1
fi
if "$BIN" '-oProxyCommand=x' 2>/dev/null; then
  echo "FAIL: options not refused"; exit 1
fi
refused=1
echo "gate installed:"
echo "  $BIN"
echo "  $POL"
echo "  validation self-tests: refused (non-root, off-whitelist, options) = $refused"
echo "Portal: the health page's GPU-stack card can now offer 'Apply GPU-stack updates'."
