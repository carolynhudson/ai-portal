#!/usr/bin/env bash
# Nightly forge-leaf mint + stage (the portal's half of the forge's
# self-heal, bench #41). 24h leaves: expiry IS the revocation lane, so
# the leaf must rotate while the box sleeps in its own rhythm.
#
# Lane (mirrors the proven halo import lane): mint on the pi with the
# root-held CA key, scp the pair HERE, purge the pi temps. Portal never
# reaches INTO the forge box: the forge pulls this bundle over its own
# ssh lane (it clones from this host) and swaps cert.crt/cert.key into
# %USERPROFILE%\.mesh -- the age IDENTITY KEY STAYS (grants key to the
# pubkey; the hub migrates record ownership name -> new fp).
#
# Runs as the portal user via the mesh-mint-forge systemd user timer.
set -e
K="$HOME/ai/config/ssh/mesh_admin"
H="meshops@pihole.local"
D="$HOME/ai/state/mesh/forge-bundle"
umask 077
mkdir -p "$D"
test -f "$K" || { echo "portal ssh key missing; cannot mint"; exit 1; }
# The mint output stays VISIBLE (a muted first run failed silently and the
# staging dir sat empty; a timer journal must show the reason, not silence).
ssh -i "$K" -o BatchMode=yes -o ConnectTimeout=8 "$H" \
  "sudo bash -c 'step certificate create forge /tmp/pub-forge.crt /tmp/pub-forge.key --profile leaf --ca /etc/step-ca/certs/root_ca.crt --ca-key /etc/step-ca/secrets/root_ca_key --ca-password-file /etc/step-ca/secrets/password --san forge --not-before 0m --not-after 24h --no-password --insecure -f && chmod 600 /tmp/pub-forge.key && chown meshops /tmp/pub-forge.*'"
scp -i "$K" -q "$H:/tmp/pub-forge.crt" "$D/cert.crt"
scp -i "$K" -q "$H:/tmp/pub-forge.key" "$D/cert.key"
ssh -i "$K" -o BatchMode=yes "$H" 'sudo rm -f /tmp/pub-forge.*'
chmod 600 "$D"/cert.crt "$D"/cert.key
END=$(openssl x509 -in "$D/cert.crt" -noout -enddate | cut -d= -f2)
FP=$(openssl x509 -in "$D/cert.crt" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':')
echo "forge leaf staged $(date -Is) expires $END fp ${FP:0:12}"
