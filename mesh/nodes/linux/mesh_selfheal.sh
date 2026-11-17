#!/usr/bin/env bash
# mesh_selfheal.sh - node-cert self-heal at launch (Linux port of the
# forge's mesh_selfheal.ps1, bench #43; docs/mesh-forge-enroll.md).
# The portal mints a 24 h leaf nightly PER NODE and stages it at
# halo:~/ai/state/mesh/<node>-bundle/ (expiry = revocation, so stale
# bundles are refused). Rule: cert notAfter < MIN_HOURS_LEFT -> pull to
# TEMP copies, enroll from the TEMP (meshctl copies INTO ~/.mesh/
# cert.crt: feeding it the destination = sharing violation), KEEP the
# age identity key untouched (grants ride the pubkey), purge temps,
# verify. Failure-tolerant by design: meshd/router keep working on the
# file fallback.
set -u
MIN_HOURS_LEFT="${MIN_HOURS_LEFT:-6}"
NODE="${MESH_NODE:-$(hostname | tr 'A-Z' 'a-z')}"
ROLES="${MESH_ROLES:-node}"
M="${MESHD_MESHCTL:-$HOME/.local/bin/meshctl}"
MESHDIR="$HOME/.mesh"
ROOT_CA="${MESH_ROOT_CA:-$HOME/.mesh/root_ca.crt}"
STAGED="${MESH_STAGED:-halo.local:~/ai/state/mesh/${NODE}-bundle}"
SSH_KEY="${MESH_SSH_KEY:-}"
LOG="${MESH_HEAL_LOG:-${MESHD_ROOT:-$HOME/forge}/logs/mesh_selfheal.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null
log() { echo "$(date -Is)  $*" | tee -a "$LOG"; }

[ -d "$MESHDIR" ] || { log "MESHDIR MISSING - nothing to heal"; exit 0; }

hours_left=-1
if [ -f "$MESHDIR/cert.crt" ]; then
  exp=$(openssl x509 -in "$MESHDIR/cert.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -n "${exp:-}" ]; then
    hours_left=$(( ( $(date -d "$exp" +%s) - $(date +%s) ) / 3600 ))
    if [ "$hours_left" -ge "$MIN_HOURS_LEFT" ] && [ "${FORCE:-0}" != 1 ]; then
      log "cert fresh: ${hours_left} h left (need ${MIN_HOURS_LEFT}) - no heal"
      exit 0
    fi
    log "cert low: ${hours_left} h left - healing"
  else
    log "cert unreadable - healing"
  fi
else
  log "no cert.crt - healing"
fi

TMP=$(mktemp -d "$MESHDIR/heal.XXXXXX")
SCPE="scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
[ -n "$SSH_KEY" ] && SCPE="$SCPE -i $SSH_KEY"
# shellcheck disable=SC2086
$SCPE "$STAGED/cert.crt" "$TMP/cert.crt" >/dev/null 2>&1
# shellcheck disable=SC2086
$SCPE "$STAGED/cert.key" "$TMP/cert.key" >/dev/null 2>&1
if [ ! -s "$TMP/cert.crt" ] || [ ! -s "$TMP/cert.key" ]; then
  rm -rf "$TMP"
  log "PULL FAILED (halo offline or bundle absent) - continuing on file fallback"
  exit 0
fi
chmod 600 "$TMP/cert.key"

newexp=$(openssl x509 -in "$TMP/cert.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
newh=$(( ( $(date -d "$newexp" +%s) - $(date +%s) ) / 3600 ))
if [ "$newh" -lt 1 ]; then
  rm -rf "$TMP"
  log "bundle STALE (${newh} h left) - refused (expiry = revocation law)"
  exit 0
fi

"$M" enroll --name "$NODE" --cert-file "$TMP/cert.crt" --key-file "$TMP/cert.key" \
  --root-ca "$ROOT_CA" --roles "$ROLES" >>"$LOG" 2>&1
rc=$?
rm -f "$TMP/cert.key" "$TMP/cert.crt"
rmdir "$TMP" 2>/dev/null
if [ "$rc" -ne 0 ]; then
  log "ENROLL FAILED rc=$rc - continuing on file fallback"
  exit 0
fi
len=$("$M" get subagent-key 2>/dev/null | wc -c)
log "healed: leaf ${newh} h left; hub lane live (key length ${len}, never printed)"
