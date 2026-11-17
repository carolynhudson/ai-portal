import json, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# llama_probe.py - prove the prose endpoint answers and measure its tok/s.
# Reads the API key from C:\forge\llama\api_key.txt (never prints it).
BASE = "http://127.0.0.1:8081"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else (
    "Rewrite this as one vivid 60-word image prompt: a red apple on a marble counter, morning light.")

key = open(r"C:\forge\llama\api_key.txt", encoding="ascii").read().strip()
body = json.dumps({
    "model": "forge-prose",
    "messages": [{"role": "user", "content": PROMPT}],
    "max_tokens": 200, "temperature": 0.8, "stream": False,
}).encode()

t0 = time.time()
req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                             headers={"Content-Type": "application/json",
                                      "Authorization": "Bearer " + key})
with urllib.request.urlopen(req, timeout=600) as r:
    out = json.load(r)
dt = time.time() - t0
u = out.get("usage", {})
n = u.get("completion_tokens", 0)
print("reply chars:", len(out["choices"][0]["message"]["content"]))
print("completion tokens:", n, "wall %.1f s" % dt, "-> %.1f tok/s (incl. prefill)" % (n / dt if dt > 0 else 0))
print("model:", out.get("model"))
print("--- reply head ---")
print(out["choices"][0]["message"]["content"][:400])
