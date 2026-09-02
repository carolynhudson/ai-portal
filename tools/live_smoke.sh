#!/usr/bin/env bash
set -uo pipefail
cd ~/ai-projects/ai-portal
export AI_PORTAL_PASSWORD=livetestpw
PORT=18090
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >/tmp/aipp-live.log 2>&1 &
SRV=$!
# wait for boot
for i in $(seq 1 30); do
  if curl -fsSL "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
J() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "=== /healthz ==="; curl -s "http://127.0.0.1:$PORT/healthz"; echo
echo "=== /api/status (unauth) ==="; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:$PORT/api/status"
echo "=== login (wrong) ==="; curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://127.0.0.1:$PORT/api/login" -H 'content-type: application/json' -d '{"password":"nope"}'
echo "=== login (right) -> cookie ==="
curl -s -c /tmp/aipp-cookies.txt -o /dev/null -w "%{http_code}\n" -X POST "http://127.0.0.1:$PORT/api/login" -H 'content-type: application/json' -d '{"password":"livetestpw"}'
echo "=== / (dashboard) status code ==="; curl -s -b /tmp/aipp-cookies.txt -o /tmp/aipp-dash.html -w "%{http_code}\n" "http://127.0.0.1:$PORT/"
echo "dashboard shows providers:"; grep -oE "llama.cpp|ComfyUI|Hugging Face" /tmp/aipp-dash.html | sort -u
echo "dashboard health keys present (reboot badge only when pending):"; grep -ioE "reboot required|vulkan sdk" /tmp/aipp-dash.html | sort -u
echo "=== /api/status providers summary ==="
curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/status" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for k,v in d['providers'].items():
    print(' ',k,'| installed',v['installed'],'| svc',v['service_state'],'| healthy',v['healthy'])
print('  health:', {x:d['health'][x] for x in ('reboot_required','llama_local','llama_origin','llama_behind')})
"
echo "=== /api/models live count ==="
curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/models" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  live',len(d['live']),'disk',len(d['disk']))"
echo "=== /api/metrics keys ==="
curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/metrics" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  cpu',round(d['cpu_percent'],1),'mem%',round(d['memory']['percent'],1),'gpu',bool(d.get('gpu')))"
kill $SRV 2>/dev/null
echo "=== server log (tail) ==="; tail -5 /tmp/aipp-live.log
