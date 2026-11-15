#!/usr/bin/env bash
# meshhub store backstop -- runs at the tail of pihole-sync.sh (nightly
# 03:15 root cron on the primary Pi). Purely additive; touches no service.
#
# Why this exists: the hub's per-mutation mirror keeps pi2 identical to pi,
# which survives a pi disk death but NOT corruption that propagates through
# the mirror (a bad mutation mirrors instantly). The dated snapshot is the
# time machine: 7 tarballs, kept OFF the primary (pi2 /root), sha256-sealed,
# verified at apply. Blobs are age ciphertext; the metadata carries no
# plaintext. Restore = unpack newest into place + restart meshhub (an
# owner-scheduled event, never reflex).
set -euo pipefail

[ -d /var/lib/meshhub/store ] || exit 0   # standby box: nothing to snapshot

D=/root/meshhub-snapshots
mkdir -p "$D"
TS=$(date -u +%Y%m%dT%H%M%SZ)
T="$D/meshhub-store-$TS.tgz"

# --warning=no-file-changed + tolerate tar exit 1 ("file changed as we read
# it"): the hub writes per mutation; a torn snapshot of a 20K store at 03:15
# is astronomically unlikely and the sha256 verify at apply catches it.
if ! tar --warning=no-file-changed -czf "$T" -C /var/lib/meshhub store \
        -C /etc/meshhub config.json; then
  rc=$?; [ "$rc" -eq 1 ] || exit "$rc"
fi
sha256sum "$T" > "$T.sha256"

# prune on pi: newest 7
ls -1t "$D"/meshhub-store-*.tgz 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$D"/meshhub-store-*.sha256 2>/dev/null | tail -n +8 | xargs -r rm -f

# ship off-primary: root -> meshops staging + sudo apply (pi2 root inbound locked)
# -n only for command hops: rsync's remote shell needs stdin for its protocol
# (same SSH/RSH split as pihole-sync.sh).
KEY=/root/.ssh/id_ed25519_sync
SSH="ssh -n -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
RSH="ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
$SSH meshops@pihole2.local 'mkdir -p ~/meshhub-snap-staging'
rsync -aq -e "$RSH" "$T" "$T.sha256" meshops@pihole2.local:meshhub-snap-staging/
$SSH meshops@pihole2.local 'sudo /usr/local/bin/meshhub-snap-apply.sh'
echo "SNAP-OK $T"
