#!/usr/bin/env bash
# register_autostart.sh - meshd as a systemd USER service (the Linux
# equivalent of the forge's logon tasks). Boot-without-login law:
# Linger must be on (prints the command; it needs root - owner lane).
set -eu
MESHD_ROOT="${MESHD_ROOT:-$HOME/forge}"
UNIT="$HOME/.config/systemd/user/meshd.service"
mkdir -p "$HOME/.config/systemd/user" "$MESHD_ROOT/logs"
cat > "$UNIT" <<EOF
[Unit]
Description=HALO mesh node daemon (meshd)
After=network-online.target

[Service]
EnvironmentFile=$MESHD_ROOT/etc/meshd.env
ExecStart=$MESHD_ROOT/bin/meshd status-server --bind \${MESHD_BIND} --port 8092
Restart=on-failure
RestartSec=5
StandardOutput=append:$MESHD_ROOT/logs/meshd.log
StandardError=append:$MESHD_ROOT/logs/meshd.log

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now meshd.service
echo "meshd: $(systemctl --user is-active meshd.service)"
if [ "$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)" != "yes" ]; then
  echo "NOTE: Linger off - meshd dies at logout. Owner: loginctl enable-linger $(id -un)"
fi
