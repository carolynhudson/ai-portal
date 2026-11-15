#!/usr/bin/env bash
# root-side apply on pi2 for meshhub store snapshots staged by pi into
# /home/meshops/meshhub-snap-staging (the standard pair lane: pi2 root
# inbound is locked, so root ships via meshops staging + this sudo apply).
# Installs to the SAME path as the primary uses (/root/meshhub-snapshots)
# so the shipped .sha256 verifies verbatim. Verifies before declaring OK:
# a snapshot that cannot be read is not a backstop.
set -euo pipefail

S=/home/meshops/meshhub-snap-staging
D=/root/meshhub-snapshots
install -d -m 700 "$D"

shopt -s nullglob
n=0
for f in "$S"/*.tgz "$S"/*.sha256; do
  install -m 600 -o root -g root "$f" "$D/"
  n=$((n+1))
done
rm -f "$S"/*

# prune: newest 7
ls -1t "$D"/meshhub-store-*.tgz 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$D"/meshhub-store-*.sha256 2>/dev/null | tail -n +8 | xargs -r rm -f

# verify newest: seal + readability
NEW=$(ls -1t "$D"/meshhub-store-*.tgz 2>/dev/null | head -1)
if [ -z "$NEW" ]; then
  echo "SNAP-APPLY: staged nothing (n=$n)"; exit 0
fi
sha256sum -c "$NEW.sha256"
tar -tzf "$NEW" >/dev/null
echo "SNAP-APPLY-OK $NEW (staged $n)"
