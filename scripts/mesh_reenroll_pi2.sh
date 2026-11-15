#!/bin/sh
# Daily pihole2 node-cert re-enroll (root cron on pihole.local).
# Mint a fresh 24h leaf from the mesh root, ship it over the proven
# root->meshops staging channel, enroll on pi2. Identity age key on
# pi2 is stable; the hub rotates the fingerprint, ownership migrates.
set -e
step certificate create pihole2 /tmp/mre.crt /tmp/mre.key \
  --profile leaf --ca /etc/step-ca/certs/root_ca.crt \
  --ca-key /etc/step-ca/secrets/root_ca_key \
  --ca-password-file /etc/step-ca/secrets/password \
  --san pihole2 --not-before 0m --not-after 24h \
  --no-password --insecure -f >/dev/null
chmod 600 /tmp/mre.key
rsync -q -e "ssh -i /root/.ssh/id_ed25519_sync -o BatchMode=yes" \
  /tmp/mre.crt /tmp/mre.key meshops@pihole2.local:~/
ssh -o BatchMode=yes -i /root/.ssh/id_ed25519_sync meshops@pihole2.local \
  "sudo bash -c 'mv /home/meshops/mre.crt /root/.mre.crt; mv /home/meshops/mre.key /root/.mre.key; chmod 600 /root/.mre.key; HOME=/root meshctl enroll --name pihole2 --cert-file /root/.mre.crt --key-file /root/.mre.key --root-ca /root/root_ca.crt --roles hub-standby --hub pihole.home.lan:9443; rm -f /root/.mre.crt /root/.mre.key'"
rm -f /tmp/mre.crt /tmp/mre.key
echo "pihole2 re-enrolled $(date -Is)"
