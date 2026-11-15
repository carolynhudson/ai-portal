#!/usr/bin/env bash
# Mint a one-time mesh enrollment token (root on pihole.local).
# usage: mesh-token.sh <node-name>
set -e
step ca token "$1" \
  --provisioner mesh \
  --key /etc/step-ca/keys/mesh.json \
  --ca-url https://pihole.local:9000 \
  --root /etc/step-ca/certs/root_ca.crt \
  --expires 15m
