import json
import urllib.request

# verify_health.py : supervisor + surface checks via MCP tools/call (shape:
# forge_health returns the health payload directly).
MCP = "http://127.0.0.1:8096/mcp"
body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "forge_health", "arguments": {}}}
r = urllib.request.Request(MCP, data=json.dumps(body).encode(),
                           headers={"Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(r, timeout=60).read().decode())["result"]
h = json.loads(res["content"][0]["text"])
print("supervisor:", json.dumps(h.get("supervisor")))
print("prose_8081 (want False = retired):", h.get("prose_8081_tcp"))
print("router_8082:", h.get("router_8082_tcp"), " agent_24096:", h.get("agent_24096_tcp"))
print("engine ok:", h.get("engine", {}).get("ok"), " queue:", h.get("queue"))
