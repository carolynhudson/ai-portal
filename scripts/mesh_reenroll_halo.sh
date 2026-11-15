#!/usr/bin/env bash
# Daily halo node-cert re-enroll (24h leaves; expiry = revocation).
# Identity age key is stable (stored); the hub rotates the fingerprint
# and migrates record ownership. Runs as the portal user via the
# mesh-reenroll systemd user timer. Import lane (proven): mint on the
# pi, scp the pair, enroll, purge everywhere.
set -e
M="${MESHCTL:-$HOME/.local/bin/meshctl}"
K="$HOME/ai/config/ssh/mesh_admin"
H="meshops@pihole.local"
ROOT_CA="$HOME/ai/state/mesh/root_ca.crt"
test -x "$M" || { echo "meshctl missing"; exit 1; }
test -f "$K" || { echo "portal ssh key missing; cannot mint-renew"; exit 1; }
ssh -i "$K" -o BatchMode=yes -o ConnectTimeout=8 "$H" \
  "sudo bash -c 'step certificate create halo /tmp/pub-halo.crt /tmp/pub-halo.key --profile leaf --ca /etc/step-ca/certs/root_ca.crt --ca-key /etc/step-ca/secrets/root_ca_key --ca-password-file /etc/step-ca/secrets/password --san halo --not-before 0m --not-after 24h --no-password --insecure -f >/dev/null 2>&1 && chmod 600 /tmp/pub-halo.key && chown meshops /tmp/pub-halo.*'"
scp -i "$K" -q "$H:/tmp/pub-halo.crt" "$HOME/.mesh/new.crt"
scp -i "$K" -q "$H:/tmp/pub-halo.key" "$HOME/.mesh/new.key"
chmod 600 "$HOME/.mesh/new.key"
ssh -i "$K" -o BatchMode=yes "$H" 'sudo rm -f /tmp/pub-halo.*'
"$M" enroll --name halo --cert-file "$HOME/.mesh/new.crt" --key-file "$HOME/.mesh/new.key" \
  --root-ca "$ROOT_CA" --roles portal
"$M" nodesjson >/dev/null   # authenticated probe (hub TLS requires client certs)
echo "halo re-enrolled $(date -Is)"
