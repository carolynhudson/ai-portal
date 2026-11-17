import json
import time
import urllib.error
import urllib.request

# router_smoke_new.py : load-smoke the library models on the router with the
# new preset sections, then prose 8081 on its new path.
KEY = open(r"C:\forge\llama\subagent_key.txt", encoding="ascii").read().strip()
PKEY = open(r"C:\forge\llama\api_key.txt", encoding="ascii").read().strip()


def chat(base, key, model, max_tokens=64, timeout=600):
    r = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps({"model": model,
                         "messages": [{"role": "user",
                                       "content": "Reply with exactly: READY"}],
                         "max_tokens": max_tokens}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())
        msg = resp["choices"][0]["message"]
        u = resp.get("usage", {})
        return "OK %.1f s content=%r u=%s" % (time.time() - t0,
                                              (msg.get("content") or "")[:40],
                                              [u.get("prompt_tokens"), u.get("completion_tokens")])
    except urllib.error.HTTPError as e:
        return "HTTP %d after %.1f s body=%s" % (e.code, time.time() - t0,
                                                 e.read().decode()[:200])


R = "http://127.0.0.1:8082"
print("Qwen3-4b:", chat(R, KEY, "Qwen3-4b"))
print("gpt-oss-20b:", chat(R, KEY, "gpt-oss-20b"))
print("Qwen3-30b-A3B-Instruct (prose model via router):", chat(R, KEY, "Qwen3-30b-A3B-Instruct"))
print("swap back nemotron:", chat(R, KEY, "nemotron"))
print("prose 8081 (dedicated lane, new path):",
      chat("http://127.0.0.1:8081", PKEY, "qwen30b", max_tokens=64))
