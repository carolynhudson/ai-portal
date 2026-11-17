import json
import time
import urllib.request

# cycle_router.py : router stop/start via MCP + list ids (post-bonsai check).
MCP = "http://127.0.0.1:8096/mcp"


def rpc(tool):
    b = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": tool, "arguments": {}}}
    r = urllib.request.Request(MCP, data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(json.loads(urllib.request.urlopen(r, timeout=600).read().decode())
                      ["result"]["content"][0]["text"])


print("stop:", rpc("forge_router_stop").get("ok"))
time.sleep(3)
print("start:", rpc("forge_router_start").get("ok"))
time.sleep(5)
key = open(r"C:\forge\llama\subagent_key.txt", encoding="ascii").read().strip()
r = urllib.request.Request("http://127.0.0.1:8082/v1/models",
                           headers={"Authorization": "Bearer " + key})
rows = json.loads(urllib.request.urlopen(r, timeout=30).read().decode())["data"]
print("ids:", len(rows))
for m in rows:
    print("  ", m["id"], m.get("status", {}).get("value"))
