import json
import time
import urllib.request

# cycle_node.ps1 companion: cycle router + opencode, then verify everything
# against the new contract (2026-11-12 architecture pass).
MCP = "http://127.0.0.1:8096/mcp"
KEY = open(r"C:\forge\llama\subagent_key.txt", encoding="ascii").read().strip()
ROUTER = "http://127.0.0.1:8082"


def rpc(tool, args=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}}}
    r = urllib.request.Request(MCP, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    res = json.loads(urllib.request.urlopen(r, timeout=1800).read().decode())["result"]
    return json.loads(res["content"][0]["text"])


print("router stop:", json.dumps(rpc("forge_router_stop"))[:120])
time.sleep(4)
print("router start:", json.dumps(rpc("forge_router_start"))[:300])
time.sleep(5)

print("models (want 7, ALL unloaded = cold law):")
r = urllib.request.Request(ROUTER + "/v1/models",
                           headers={"Authorization": "Bearer " + KEY})
for m in json.loads(urllib.request.urlopen(r, timeout=30).read().decode())["data"]:
    print("   ", m["id"], m.get("status", {}).get("value"))

print("load Qwen3-4b (smallest, proves on-demand load works):")
t0 = time.time()
r = urllib.request.Request(
    ROUTER + "/v1/chat/completions",
    data=json.dumps({"model": "Qwen3-4b",
                     "messages": [{"role": "user", "content": "Reply with exactly: READY"}],
                     "max_tokens": 16}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
resp = json.loads(urllib.request.urlopen(r, timeout=300).read().decode())
msg = resp["choices"][0]["message"]
print("  %.1f s content=%r" % (time.time() - t0, (msg.get("content") or "")[:40]))

h = rpc("forge_health")
print("forge_health supervisor:", json.dumps(h["health"].get("supervisor")))
print("forge_health prose_8081 (want False = retired):", h["health"]["prose_8081_tcp"])
print("forge_health router/agent tcp:", h["health"]["router_8082_tcp"], h["health"]["agent_24096_tcp"])
