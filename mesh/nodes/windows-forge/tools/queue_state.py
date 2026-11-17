import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=10) as r:
    q = json.load(r)
print("running:", [p[1] for p in q.get("queue_running", [])])
print("pending:", [p[1] for p in q.get("queue_pending", [])])
for pid in [p[1] for p in q.get("queue_running", [])]:
    with urllib.request.urlopen("http://127.0.0.1:8188/history/" + pid, timeout=10) as r:
        h = json.load(r)
    if pid in h:
        print(pid, "done")
    else:
        print(pid, "in progress")
