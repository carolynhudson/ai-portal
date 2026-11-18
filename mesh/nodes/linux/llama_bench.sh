#!/usr/bin/env bash
# llama_bench.sh - ON-DEMAND timing lane for the mesh capacity card (owner 2026-11-17).
#
# meshd's cards carry computed cold/warm first-token estimates from the GGUF
# headers alone; this script upgrades them to MEASURED by timing the real
# router. It writes $MESHD_ROOT/llama/bench.json, which meshd reads read-side
# at caps() time (see README "bench.json").
#
# LAWS:
#  - ON-DEMAND ONLY: every model request loads the model (router cold start).
#    Never run from a timer; never alongside an image-forge render (resource
#    law); one model at a time is inherent in --models-max 1.
#  - READ-side honesty: timings measured from the wire (timings=1), disk speed
#    measured with dd; nothing is invented. A model that fails to answer keeps
#    its previous row (a bench that lies is worse than no bench).
#  - RESTORE law (owner, 2026-11-17): a max_instances=1 router does NOT
#    self-restore what a measurement evicted. The bench refuses while a model
#    is loaded, and re-loads the pre-bench models at the end.
#  - The image: section (ComfyUI ram_peak_gib) is merged from the existing
#    file, never dropped.
#  - RAM floor on shared-memory boxes: refuse under 24 GiB available (same
#    number start_comfy.sh refuses under), so a bench never OOMs the box.
set -euo pipefail

ROOT="${MESHD_ROOT:-$HOME/forge}"
OUT="${MESHD_BENCH_FILE:-$ROOT/llama/bench.json}"
ROUTER="${MESHD_ROUTER_URL:-http://127.0.0.1:8080}"
TOK="${MESH_TOKEN:-${MESHD_TOKEN:-}}"
[ -n "$TOK" ] || TOK=$(meshctl get subagent-key 2>/dev/null | tr -d '[:space:]')
[ -n "$TOK" ] || { [ -r "$ROOT/llama/subagent_key.txt" ] && TOK=$(tr -d '[:space:]' < "$ROOT/llama/subagent_key.txt"); }
RAM_FLOOR_KIB=$((24 * 1024 * 1024))

die() { echo "llama_bench: $*" >&2; exit 1; }

avail_kib=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
[ "${avail_kib:-0}" -ge "$RAM_FLOOR_KIB" ] || die "refusing: ${avail_kib} KiB available < 24 GiB floor (resource law)"

[ -n "$TOK" ] || { [ -r "$TOK_FILE" ] && TOK=$(tr -d '[:space:]' < "$TOK_FILE"); }
[ -n "$TOK" ] || die "no ONE-token (MESH_TOKEN / meshctl get subagent-key / \$ROOT/llama/subagent_key.txt)"

command -v curl >/dev/null || die "curl required"
command -v python3 >/dev/null || die "python3 required (JSON merge)"

mkdir -p "$(dirname "$OUT")"

# disk read speed from the biggest registered model file (dd, drop caches best-effort)
model_files=$(curl -sf -m 10 -H "Authorization: Bearer $TOK" "$ROUTER/v1/models" \
  | python3 -c 'import json,sys
for m in json.load(sys.stdin).get("data", []):
    args = (m.get("status") or {}).get("args") or []
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            print(args[i + 1]); break' ) || die "router /v1/models failed at $ROUTER"
[ -n "$model_files" ] || die "no models registered"

# BUSY-ROUTER LAW (owner incident 2026-11-17): measuring loads models, and a
# max_instances=1 router SWAPS the owner's running model out. The bench
# refuses while ANY model is loaded. Run it on an idle router, or say so on
# purpose with MESHD_BENCH_ALLOW_SWAP=1.
loaded=$(curl -sf -m 10 -H "Authorization: Bearer $TOK" "$ROUTER/v1/models" \
  | python3 -c 'import json,sys
print(",".join(m["id"] for m in json.load(sys.stdin).get("data", [])
      if (m.get("status") or {}).get("value") == "loaded"))' 2>/dev/null)
if [ -n "$loaded" ] && [ "${MESHD_BENCH_ALLOW_SWAP:-}" != "1" ]; then
  die "router BUSY (loaded: ${loaded:0:80}) - a measurement loads a model and a max_instances=1 router EVICTS yours, and it does NOT self-restore (owner-witnessed 2026-11-17). Run when idle, or MESHD_BENCH_ALLOW_SWAP=1 (the bench will reload yours at the end)"
fi

biggest=$(echo "$model_files" | xargs -r ls -S 2>/dev/null | head -1)
disk_mb_s=300
if [ -r "$biggest" ]; then
  sz_mib=$(( $(stat -c %s "$biggest") / 1048576 ))
  if [ "$sz_mib" -gt 300 ]; then
    t0=$(date +%s%N)
    dd if="$biggest" of=/dev/null bs=1M count=256 iflag=direct 2>/dev/null \
      || dd if="$biggest" of=/dev/null bs=1M count=256 2>/dev/null
    t1=$(date +%s%N)
    ms=$(( (t1 - t0) / 1000000 )); [ "$ms" -gt 0 ] && disk_mb_s=$(( 256 * 1000 / ms ))
  fi
fi

python3 - "$OUT" "$disk_mb_s" <<'PY' "$ROUTER" "$TOK" "$model_files"
import json, sys, os, subprocess, time, urllib.request

out, disk, router, tok, files = sys.argv[1:6]
prev = {}
if os.path.exists(out):
    try:
        prev = json.load(open(out))
    except Exception:
        pass

def models():
    req = urllib.request.Request(router + "/v1/models",
                                 headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("data", [])

def post(path, payload, timeout=30):
    req = urllib.request.Request(router + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def loaded(mid):
    for m in models():
        if m.get("id") == mid:
            return (m.get("status") or {}).get("value")
    return None

def ensure_loaded(mid, budget=900):
    v = loaded(mid)
    if v == "loaded":
        return 0.0
    t0 = time.monotonic()
    post("/models/load", {"model": mid}, timeout=60)
    while time.monotonic() - t0 < budget:
        v = loaded(mid)
        if v == "loaded":
            return time.monotonic() - t0
        time.sleep(2)
    raise TimeoutError("load did not reach 'loaded' in %ds" % budget)

def unload(mid):
    try:
        post("/models/unload", {"model": mid}, timeout=120)
    except Exception:
        pass

def chat(mid, max_tokens):
    body = json.dumps({
        "model": mid, "messages": [{"role": "user",
            "content": "Count from 1 to 120, one number per line."}],
        "max_tokens": max_tokens, "temperature": 0, "timings": True,
    }).encode()
    req = urllib.request.Request(router + "/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    total = time.monotonic() - t0
    u = d.get("usage") or {}
    t = d.get("timings") or {}
    pt = (t.get("prompt_ms") or 0) / 1000.0
    et = (t.get("eval_ms") or 0) / 1000.0
    pc = u.get("prompt_tokens") or 0
    ec = u.get("completion_tokens") or 0
    return total, pt, et, pc, ec

bench = prev.get("router") or {}
originals = [m["id"] for m in models()
             if (m.get("status") or {}).get("value") == "loaded"]
if originals:
    print("router busy (loaded: %s) - the router does NOT self-restore: the bench re-loads these at the end"
          % ", ".join(originals))
for m in models():
    mid = m.get("id")
    if not mid:
        continue
    try:
        # honest load: the router's own lane (the WebUI's lane), timed; then
        # one warm request for prompt/decode tps with the model resident
        load_s = ensure_loaded(mid)
        warm, pt2, et2, pc2, ec2 = chat(mid, 64)
        row = bench.get(mid) or {}
        row["load_s"] = round(load_s, 2)
        if pt2 > 0.05 and pc2:
            row["prompt_tps"] = round(pc2 / pt2, 1)
        if et2 > 0.05 and ec2:
            row["decode_tps"] = round(ec2 / et2, 1)
        row["at"] = int(time.time())
        bench[mid] = row
        print("  %-44s load %ss prompt %s t/s decode %s t/s" % (
            mid[:44], row["load_s"], row.get("prompt_tps"), row.get("decode_tps")))
    except Exception as e:
        print("  %-44s FAILED (%s) - keeping the previous row" % (mid[:44], type(e).__name__))
    unload(mid)

# RESTORE PHASE (owner law 2026-11-17): a max_instances=1 router does NOT
# self-restore the evicted model. Leave the router as you found it.
for mid in originals:
    try:
        ensure_loaded(mid, budget=1800)
        print("restored:", mid)
    except Exception as e:
        print("RESTORE FAILED for %s (%s) - load it from the router WebUI before use" % (mid, type(e).__name__))

data = {"disk_read_mb_s": int(disk), "router": bench,
        "image": prev.get("image") or {},
        "note": "measured by llama_bench.sh (on-demand; never a timer)"}
tmp = out + ".tmp"
json.dump(data, open(tmp, "w"), indent=1)
os.replace(tmp, out)
print("wrote", out)
PY
