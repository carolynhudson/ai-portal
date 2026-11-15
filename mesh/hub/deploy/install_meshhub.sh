#!/usr/bin/env bash
# meshhub install on a Pi (run as root; additive service, port 9443).
# usage: BIN=/path/to/meshhub install_meshhub.sh primary|standby
set -e
ROLE="${1:?usage: install_meshhub.sh primary|standby  (BIN=/path env)}"
BIN="${BIN:?BIN=/path/to/meshhub binary required}"
if [ "$ROLE" = primary ]; then
  MIRROR_ENABLED=true;  MIRROR_HOST=meshops@pihole2.local
else
  MIRROR_ENABLED=false; MIRROR_HOST=meshops@pihole.local
fi
test -x "$BIN"
install -m 755 "$BIN" /usr/local/bin/meshhub
mkdir -p /var/lib/meshhub/store/blobs /etc/meshhub
cat > /etc/meshhub/config.json <<EOF
{
  "listen": "0.0.0.0:9443",
  "tls_pem": "/etc/pihole/tls.pem",
  "root_ca": "/etc/step-ca/certs/root_ca.crt",
  "store": "/var/lib/meshhub/store",
  "takeover": "/var/lib/meshhub/TAKEOVER",
  "max_bytes": 33554432,
  "mirror": { "host": "$MIRROR_HOST", "key": "/root/.ssh/id_ed25519_sync", "enabled": $MIRROR_ENABLED }
}
EOF
chmod 600 /etc/meshhub/config.json
if [ "$ROLE" = primary ]; then touch /var/lib/meshhub/WRITER; fi
cat > /etc/systemd/system/meshhub.service <<'EOF'
[Unit]
Description=HALO mesh hub (node registry + ciphertext-only secrets broker)
After=network-online.target pihole-FTL.service step-ca.service

[Service]
ExecStart=/usr/local/bin/meshhub /etc/meshhub/config.json
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now meshhub
sleep 2
echo -n "meshhub: "; systemctl is-active meshhub
curl -sk --cacert /root/root_ca.crt https://127.0.0.1:9443/healthz; echo
