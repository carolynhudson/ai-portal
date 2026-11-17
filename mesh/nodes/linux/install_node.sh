#!/usr/bin/env bash
# install_node.sh - Linux mesh node from the tree (Ubuntu/Debian/Fedora,
# x86_64/aarch64). Mirrors the windows-forge kit: build from the tree,
# user-scope layout, no root. meshd binds 127.0.0.1:8092; the firewall
# gates its MCP door to the portal IP (README).
set -eu
MESHD_ROOT="${1:-${MESHD_ROOT:-$HOME/forge}}"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
command -v cargo >/dev/null || { echo "cargo missing: install rustup (README)"; exit 1; }
mkdir -p "$MESHD_ROOT/bin" "$MESHD_ROOT/etc/mesh" "$MESHD_ROOT/llama" "$MESHD_ROOT/logs"

echo "== build meshd + meshctl from the tree =="
(cd "$REPO/mesh/meshd" && cargo build --release)
cp -f "$REPO/mesh/meshd/target/release/meshd" "$MESHD_ROOT/bin/meshd"
(cd "$REPO/mesh/hub" && cargo build --release)
cp -f "$REPO/mesh/hub/target/release/meshctl" "$MESHD_ROOT/etc/mesh/meshctl"

ENVF="$MESHD_ROOT/etc/meshd.env"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
MESHD_ROOT=$MESHD_ROOT
MESHD_MESHCTL=$MESHD_ROOT/etc/mesh/meshctl
MESHD_NODE_NAME=$(hostname)
MESHD_ROUTER_URL=http://127.0.0.1:8082
MESHD_MCP_URL=http://127.0.0.1:8096
MESHD_SERVICES_FILE=$MESHD_ROOT/etc/services.json
MESHD_LLAMA_VERSION_FILE=$MESHD_ROOT/llama/version.json
EOF
  chmod 600 "$ENVF"
fi
[ -f "$MESHD_ROOT/etc/services.json" ] || \
  cp "$(dirname "$0")/services.linux.json" "$MESHD_ROOT/etc/services.json"

if [ ! -f "$HOME/.mesh/cert.crt" ]; then
  echo
  echo "no node identity yet (~/.mesh/cert.crt). Enroll via the portal lane:"
  echo "  1) portal stages a leaf at halo:~/ai/state/mesh/<node>-bundle/"
  echo "  2) MESH_NODE=<node> mesh_selfheal.sh   (pulls + enrolls + verifies)"
  echo "  root CA: copy halo:~/ai/state/mesh/root_ca.crt to ~/.mesh/root_ca.crt"
fi
echo
echo "done. binary: $MESHD_ROOT/bin/meshd  env: $ENVF"
echo "next: register_autostart.sh, then verify_node.sh"
