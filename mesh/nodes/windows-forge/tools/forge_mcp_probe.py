import json
import re
import sys
import urllib.request

# forge_mcp_probe.py <method> [params-json-file] : local JSON-RPC probe of
# forge_mcp (http://127.0.0.1:8096/mcp). Methods: initialize, tools/list,
# tools/call <name> via params file {"name":..., "arguments":{...}}.
method = sys.argv[1]
params = {}
if len(sys.argv) > 2:
    params = json.load(open(sys.argv[2], encoding="utf-8"))
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
req = urllib.request.Request("http://127.0.0.1:8096/mcp", data=body,
                             headers={"Content-Type": "application/json",
                                      "Accept": "application/json, text/event-stream"})
with urllib.request.urlopen(req, timeout=1800) as r:
    raw = r.read().decode("utf-8", "replace")
m = re.search(r"data: (\{.*\})", raw)
payload = json.loads(m.group(1)) if m else json.loads(raw)
if "error" in payload:
    print("RPC_ERROR", json.dumps(payload["error"]))
    sys.exit(1)
res = payload.get("result", payload)
if method == "tools/list":
    for t in res.get("tools", []):
        print(t["name"], "-", t["description"][:80])
else:
    for c in res.get("content", []):
        print(c.get("text", ""))
    if res.get("isError"):
        print("(isError)")
        sys.exit(1)
