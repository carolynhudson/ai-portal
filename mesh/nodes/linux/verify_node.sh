#!/usr/bin/env bash
# verify_node.sh - the node's own gate. Prints PASS/FAIL lines; exit 0
# only when all pass. Read-only. A FAIL is a hypothesis: read the
# artifact (bench #46 law) - each line names what it measured.
set -u
PORT="${MESHD_PORT:-8092}"
ROOT="${MESHD_ROOT:-$HOME/forge}"
M="${MESHD_MESHCTL:-$HOME/.local/bin/meshctl}"
fail=0
ok()  { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; fail=1; }

h=$(curl -s -m 4 "http://127.0.0.1:$PORT/healthz" || true)
v=$(echo "$h" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('version','?'))
except Exception: print('')" 2>/dev/null)
[ -n "$v" ] && ok "meshd /healthz answers (v$v)" || bad "meshd /healthz unreachable on :$PORT"

if [ -f "$HOME/.mesh/cert.crt" ]; then
  exp=$(openssl x509 -in "$HOME/.mesh/cert.crt" -noout -enddate 2>/dev/null | cut -d= -f2)
  hleft=$(( ( $(date -d "$exp" +%s) - $(date +%s) ) / 3600 ))
  [ "$hleft" -ge 6 ] && ok "cert ${hleft} h left" || bad "cert ${hleft} h left - run mesh_selfheal.sh"
else
  bad "no ~/.mesh/cert.crt - enroll first (README)"
fi

if [ -x "$M" ] && [ -f "$HOME/.mesh/cert.crt" ]; then
  if "$M" nodes >/dev/null 2>&1; then ok "hub lane live (meshctl nodes)"; else bad "hub lane 401/down (meshctl nodes)"; fi
  n=$("$M" get subagent-key 2>/dev/null | wc -c)
  [ "$n" -ge 16 ] && ok "ONE-token readable (${n} chars, not printed)" || bad "ONE-token unreadable"
else
  bad "meshctl missing at $M"
fi

vf="${MESHD_LLAMA_VERSION_FILE:-$ROOT/llama/version.json}"
if [ -f "$vf" ]; then ok "llama version record: $(python3 -c "import json;print(json.load(open('$vf')).get('version','?'))" 2>/dev/null)"
else ok "llama version: none recorded (null = no claim)"; fi

exit $fail
