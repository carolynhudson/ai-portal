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
echo "=== /api/updates/llamacpp ==="
curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/updates/llamacpp" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  current', d['current'], '| head', (d['repo_head'] or '—')[:12],
      '| releases', len(d['releases']), '| vulkan', (d['vulkan_sdk'] or '').rsplit('/',1)[-1])
r=d.get('remote') or {}
if r.get('error'):
    print('  upstream', r['error'])
else:
    print('  upstream master', (r.get('master_head') or '?')[:12],
          '| behind master', r.get('behind_master'),
          '| release', r.get('latest_release'),
          '| behind release', r.get('behind_release'),
          '| notes', r.get('release_url'))
"
echo "=== no-op update (target == deployed release => job succeeds, no build/swap) ==="
CUR=$(curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/updates/llamacpp" | python3 -c "import sys,json;print(json.load(sys.stdin)['current_commit'] or '')")
if [ -z "$CUR" ]; then
  echo "  skipped: no deployed release recorded"
else
  JOB=$(curl -s -b /tmp/aipp-cookies.txt -X POST -H 'content-type: application/json' \
        -d "{\"target\":\"$CUR\",\"restart\":false}" "http://127.0.0.1:$PORT/api/updates/llamacpp" \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('job') or '')")
  echo "  job: $JOB"
  for i in $(seq 1 60); do
    ST=$(curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/jobs/$JOB" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'])")
    [ "$ST" != "running" ] && break
    sleep 2
  done
  curl -s -b /tmp/aipp-cookies.txt "http://127.0.0.1:$PORT/api/jobs/$JOB" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  job', d['status'])
print('\n'.join('  | ' + ln for ln in (d['log'] or '').splitlines()[-10:]))
assert d['status'] == 'success', 'no-op update job did not succeed'
assert 'no-op' in (d['log'] or ''), 'expected a no-op result in the log'
print('  OK: no-op update proved the pipeline path (fetch -> resolve -> no build/swap)')
"
fi
kill $SRV 2>/dev/null
echo "=== server log (tail) ==="; tail -5 /tmp/aipp-live.log
