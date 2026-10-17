#!/usr/bin/env bash
# The embedding sidecar: a standalone llama-server for EmbeddingGemma 2, on a spare
# port, CPU, outside the router — by design.
#
#   tools/embed_sidecar.sh start|stop|status|test
#
# Why a sidecar and not the router: the router runs --models-max 1 on purpose (the
# owner's OOM history: a model in the router is an agent's eviction). An embedding
# call must never cost an agent its model, so embeddings live here: ~0.3 GB, CPU,
# ready in ~2s, and the endpoint is plain /v1/embeddings — when the NPU ONNX path
# lands, this script's occupant changes and app/core/embeddings.py does not.
#
# The model file is NOT in the router's --models-dir: the router scans that
# directory and cataloging an embedding model there invites exactly the swap
# this design exists to avoid.
set -euo pipefail

BIN_DIR="${EMBED_BIN_DIR:-/home/cortana/ai/releases/llama.cpp-b11669}"
MODEL="${EMBED_MODEL:-/home/cortana/ai/models/sidecar/embeddinggemma-2-Q8_0.gguf}"
PORT="${EMBED_PORT:-8390}"
PIDFILE="/tmp/embed-sidecar-${PORT}.pid"
LOGDIR="${HOME}/ai/logs"
LOG="${LOGDIR}/embed-sidecar.log"

ram_ok() {
  # 4 GiB available is the floor: the sidecar needs ~0.3 GB and the box needs
  # room to say no gracefully. Measured cost, not a promise: check free(1).
  local avail_kb
  avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
  [ "$avail_kb" -ge $((4 * 1024 * 1024)) ]
}

# The unit is the durable owner (installed Oct 2026): this script defers to it
# so there is exactly one way the port gets a server, no double-launch races.
UNIT=ai-embed.service
unit_installed() { [ -f "$HOME/.config/systemd/user/$UNIT" ]; }
unit_active() { systemctl --user is-active --quiet "$UNIT" 2>/dev/null; }
health_wait() {
  for _ in $(seq 1 40); do
    sleep 1
    # -f matters: llama-server answers /health with 503 "loading" while the
    # model warms; curl without -f reports success for that body, and every
    # caller then races a warming server (measured: FTS-only searches that
    # looked correct because FTS carried them, not the vectors).
    curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0
  done
  return 1
}

cmd="${1:-status}"
case "$cmd" in
  start)
    if unit_installed && unit_active; then
      echo "sidecar up: systemd unit $UNIT, port $PORT (systemctl --user to manage)"; exit 0
    fi
    if unit_installed; then
      systemctl --user start "$UNIT" &
      health_wait && { echo "sidecar up: systemd unit $UNIT, port $PORT"; exit 0; }
      echo "the unit did not answer /health within 40s; journalctl --user -u $UNIT"
      exit 4
    fi
    [ -x "$BIN_DIR/llama-server" ] || { echo "no llama-server at $BIN_DIR (EMBED_BIN_DIR)"; exit 2; }
    [ -f "$MODEL" ] || { echo "no model at $MODEL (EMBED_MODEL)"; exit 2; }
    ram_ok || { echo "refused: < 4 GiB available — the sidecar starts when the box has room"; exit 3; }
    mkdir -p "$LOGDIR"
    nohup "$BIN_DIR/llama-server" \
      --model "$MODEL" --alias embeddinggemma2 \
      --embeddings --pooling mean \
      --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 \
      --n-gpu-layers 0 \
      --host 127.0.0.1 --port "$PORT" --log-disable \
      >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    health_wait && {
      echo "sidecar up: port $PORT (pid $(cat "$PIDFILE"), $(du -h "$MODEL" | cut -f1), CPU)"; exit 0; }
    echo "the sidecar did not answer /health within 40s; see $LOG"
    exit 4
    ;;
  stop)
    if unit_installed && unit_active; then
      systemctl --user stop "$UNIT" && echo "sidecar stopped (unit $UNIT)"
      exit 0
    fi
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "sidecar stopped"
    else
      rm -f "$PIDFILE"; echo "sidecar was not running"
    fi
    ;;
  status)
    if unit_active; then
      echo "running: systemd unit $UNIT, port $PORT"
      curl -s -m 3 "http://127.0.0.1:$PORT/health" && echo
      exit 0
    fi
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running: pid $(cat "$PIDFILE"), port $PORT"
      curl -s -m 3 "http://127.0.0.1:$PORT/health" && echo
    else
      echo "not running"
    fi
    ;;
  test)
    curl -s -m 20 "http://127.0.0.1:$PORT/v1/embeddings" \
      -H "Content-Type: application/json" \
      -d '{"model":"embeddinggemma2","input":["task: search result | query: test"],"encoding_format":"float"}' \
      | python3 -c 'import json,sys,math; d=json.load(sys.stdin); v=d["data"][0]["embedding"]; print("dims:", len(v), "norm:", round(math.sqrt(sum(x*x for x in v)),4))'
    ;;
  *)
    echo "usage: $0 start|stop|status|test"; exit 1 ;;
esac
