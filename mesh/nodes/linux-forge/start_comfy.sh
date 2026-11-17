#!/usr/bin/env bash
# start_comfy.sh - ComfyUI launcher with RESOURCE GATES (owner law
# 2026-11-17: "be careful with RAM on halo... you don't want to kill
# yourself with an OOM"). Unified-memory nodes share RAM with the llama
# router; the mesh law says a router task and an image render never run
# concurrently.
#
# Gates REFUSE. They never unload the router, never kill anything -
# unloading a loaded model is the owner's migration lane.
#   gate 1: available RAM < FORGE_RAM_FLOOR_GIB (default 24) -> refuse
#   gate 2: llama-server resident with RSS > 4 GiB (a model is loaded) -> refuse
# Exits non-zero with the reason; a clean pass execs ComfyUI.
set -eu
ROOT="${FORGE_ROOT:-$HOME/comfyui}"
FLOOR_GIB="${FORGE_RAM_FLOOR_GIB:-24}"
ROUTER_RSS_LIMIT_KIB=$(( 4 * 1024 * 1024 ))   # 4 GiB
DRY="${DRY:-0}"

avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
if [ "$avail_kb" -lt $(( FLOOR_GIB * 1024 * 1024 )) ]; then
  echo "REFUSED: available RAM $(( avail_kb / 1024 / 1024 )) GiB < floor ${FLOOR_GIB} GiB (shared-memory law)"
  exit 1
fi
rss=$(pgrep -x llama-server >/dev/null 2>&1 && \
      ps -o rss= -C llama-server | awk '{s+=$1} END {print s+0}' || echo 0)
if [ "$rss" -gt "$ROUTER_RSS_LIMIT_KIB" ]; then
  echo "REFUSED: llama-router has a model resident ($(( rss / 1024 / 1024 )) GiB RSS) -"
  echo "mesh law: router work and image render are exclusive. Unloading models is the owner's lane."
  exit 1
fi
echo "gates pass: available $(( avail_kb / 1024 / 1024 )) GiB (floor ${FLOOR_GIB}), router RSS $(( rss / 1024 )) MiB"
[ "$DRY" = 1 ] && { echo "DRY: would exec ComfyUI"; exit 0; }
cd "$ROOT/ComfyUI"
exec "$ROOT/venv/bin/python" main.py --listen 127.0.0.1 --port 8188 \
  >> "$ROOT/logs/comfyui.log" 2>&1
