import json
import os
import subprocess
import sys
import time
import urllib.request

# router_probe.py : validate llama.cpp router mode on this box. Probes:
# preset key acceptance (spec-* untested per portal KB 4.10.2), model listing,
# on-demand load, MTP stats for nemotron, mutual exclusion with models-max 1
# (flint load unloads nemotron), swap latency. Port 8126, no key (local probe).

PORT = 8126
SERVER = r"C:\forge\llama\llama-server.exe"
LOG = r"C:\forge\logs\router_probe.log"
ARGS = [SERVER, "--models-dir", r"C:\forge\llama\router\models",
        "--models-preset", r"C:\forge\llama\router\presets.ini",
        "--models-max", "1", "--host", "127.0.0.1", "--port", str(PORT),
        "-t", "16", "-ngl", "0", "-fa", "on", "--jinja",
        "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0",
        "-c", "262144", "-np", "1", "--no-webui"]


def req(path, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                               headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(r, timeout=timeout)
    return json.loads(resp.read().decode())


def chat(model, content, tools=None, max_tokens=512):
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    return req("/v1/chat/completions", body, timeout=900)


def main():
    # fence the HF hub-cache scan: llama.cpp's discovery follows HF_HUB_CACHE
    # (no CLI flag exists; portal KB 4.9.1 "the fence does not fence it").
    # Point it at an empty forge dir so ONLY models-dir dirs are public ids.
    cache_fence = r"C:\forge\llama\hf-cache"
    os.makedirs(cache_fence, exist_ok=True)
    env = dict(os.environ, HF_HUB_CACHE=cache_fence)
    log = open(LOG, "wb")
    proc = subprocess.Popen(ARGS, stdout=log, stderr=subprocess.STDOUT, env=env)
    fails = []
    try:
        t0 = time.time()
        ready = False
        while time.time() - t0 < 300:
            try:
                req("/health", timeout=5)
                ready = True
                break
            except Exception:
                if proc.poll() is not None:
                    print("ROUTER DIED at startup, exit", proc.returncode)
                    dump_log()
                    return 1
                time.sleep(2)
        if not ready:
            print("router never became ready")
            dump_log()
            return 1
        print("router listening after %.1f s" % (time.time() - t0))

        models = req("/v1/models")
        ids = [m["id"] for m in models.get("data", [])]
        print("models listed:", ids)
        if "flint" not in ids or "nemotron" not in ids:
            fails.append("model listing incomplete: %s" % ids)

        tools = [{"type": "function", "function": {
            "name": "calculator", "description": "Evaluate an arithmetic expression",
            "parameters": {"type": "object", "properties": {"expr": {"type": "string"}},
                           "required": ["expr"]}}}]
        t0 = time.time()
        r = chat("nemotron", "What is 23 times 47? Use the calculator tool.",
                 tools=tools, max_tokens=1024)
        msg = r["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        print("nemotron first-request (load+answer): %.1f s, tool_calls=%d args=%s, thinking=%d chars"
              % (time.time() - t0, len(tcs),
                 (tcs[0]["function"]["arguments"] if tcs else "-"),
                 len(msg.get("reasoning_content") or "")))
        if not tcs:
            fails.append("nemotron: no tool call")

        p = req("/completion", {"model": "nemotron",
                                "prompt": "Write a Python function fizzbuzz(n) returning the classic list.\n",
                                "n_predict": 128, "temperature": 1.0, "top_p": 0.95},
                timeout=900)
        tm = p.get("timings", {})
        print("nemotron decode via router (preset MTP?): %.1f t/s"
              % tm.get("predicted_per_second", 0))

        t0 = time.time()
        r = chat("flint", "In one short sentence: what is a chroma key plate?", max_tokens=2048)
        msg = r["choices"][0]["message"]
        print("flint first-request (swap: nemotron->flint): %.1f s, reasoning=%s, content=%r"
              % (time.time() - t0, bool(msg.get("reasoning_content")),
                 (msg.get("content") or "")[:90]))
        if not msg.get("content"):
            fails.append("flint: empty content")

        t0 = time.time()
        r = chat("nemotron", "Reply with exactly: READY", max_tokens=256)
        print("nemotron swap back: %.1f s" % (time.time() - t0))

        models = req("/v1/models")
        print("model states after swaps:",
              [(m["id"], m.get("state", "?")) for m in models.get("data", [])])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        time.sleep(3)
    dump_log()
    print("VERDICT:", "PASS" if not fails else "FAIL " + "; ".join(fails))
    return 0 if not fails else 1


def dump_log():
    txt = open(LOG, encoding="utf-8", errors="replace").read()
    marks = ["not recognized", "preset", "load_startup", "draft", "accept",
             "speculative", "auto-delete", "unload", "loading model", "startup"]
    hits = [ln for ln in txt.splitlines() if any(m in ln.lower() for m in marks)]
    print("--- log highlights (%d lines) ---" % len(hits))
    for ln in hits[-40:]:
        print(ln[:180])


if __name__ == "__main__":
    sys.exit(main())
