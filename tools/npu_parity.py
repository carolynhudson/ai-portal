#!/usr/bin/env python3
"""Parity benchmark: where should the embedding workload run, measured under the agent.

    .venv/bin/python tools/npu_parity.py \
        --agent-model /path/to/agent.gguf --agent-gpu-layers 99 \
        --embed-endpoint cpu=http://127.0.0.1:8390/v1 \
        --embed-endpoint npu=http://127.0.0.1:8391/v1 \
        [--embed-endpoint gpu=http://127.0.0.1:8392/v1] \
        [--skip-agent] [--batch 64] [--reps 3]

Run it with the portal UI and agent sessions closed (the slot_bench.py contract):
it starts its OWN llama-server for the agent model on a spare port and never
touches the running router, presets.ini, or the portal database.

The question this answers, stated as the owner stated it: two configurations at
parity — (A) GPU runs the agent and the embedding workload runs on [CPU / GPU /
NPU] — same benchmark, same texts, same prompts, side by side. The claim to test
is NOT "the NPU is faster in the abstract"; it is:

  1. embedding throughput and latency at the arm (docs/s, p50, p95);
  2. what that costs the AGENT, concurrently (prefill and decode tok/s while
     embeddings stream): the NPU adds no memory bandwidth — the shared LPDDR5X
     is the real contention, so the honest metric is agent speed under load,
     not spec TOPS.

Every arm is the same OpenAI /v1/embeddings client, so any occupant of the
endpoint (CPU llama-server, GPU llama-server, an NPU ONNX runner) is measured
with the same yardstick; the day the NPU stack exposes one, this script already
speaks it.

Refusals (deliberate, slot_bench's discipline):
  * an agent model that does not fit the aperture/RAM free right now -> skipped, not OOM'd
  * an endpoint that is not answering -> the arm is reported dead, not retried forever
  * a busy agent server -> the run refuses; a shared server is a contaminated measurement

Writes only ~/ai/logs/npu-parity-<UTC timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

EMBED_TEXTS = [
    # A realistic memory-mix: short facts, medium notes, long paragraphs.
    "The router runs --models-max 1; loading a model evicts the other.",
    "IQ3_XXS tuning: min-p 0.0, top-k 20, temp 1.0, flash-attn on, lazy-mode on.",
    ("The owner's rule: never swap the router's loaded model from inside a session, "
     "because agent sessions on this box answer FROM the router's model. Swaps belong "
     "to a quiescent machine with the sessions closed, through tools/model_swap.py."),
    ("EmbeddingGemma 2 uses asymmetric task prefixes: queries are prefixed with "
     "'task: search result | query:' and documents with 'title: T | text: C'; the "
     "code-retrieval prefix is 'task: code retrieval | query:'. Omitting them works "
     "and quietly loses precision, and MRL truncation below 768d must re-normalize "
     "the vector after slicing — a sliced vector that is not renormalized degrades "
     "ranking silently, with plausible scores, not an error."),
    ("The 108 GiB shared-RAM aperture ceiling was chosen after stability testing; "
     "larger apertures and larger quants caused out-of-memory kills of the router "
     "and sessions before lazy-mode and the GSQ-RCO IQ3 quants."),
]

PROMPT = ("You are a coding agent. Summarize the constraints of running a 100-billion-parameter "
          "local model on a 122 GB unified-memory machine in five bullets: "
          + " ".join(EMBED_TEXTS))


def _post(url: str, body: dict, timeout: float = 120.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode()


# ------------------------------------------------------------------ embedding arm

def embed_bench(label: str, base: str, batch: int, reps: int) -> dict:
    """Embed the corpus `batch` texts per request, `reps` rounds, report docs/s and
    p50/p95 per-request latency. A dead endpoint is reported, not retried."""
    base = base.rstrip("/")
    out = {"label": label, "endpoint": base, "ok": False}
    texts = (EMBED_TEXTS * ((batch // len(EMBED_TEXTS)) + 1))[:batch]
    payloads = [f"title: none | text: {t}" for t in texts]
    try:
        _get(base[: -len("/v1")] + "/health" if base.endswith("/v1") else base + "/health", 5)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"endpoint not answering: {e}"
        return out
    lat: list[float] = []
    dims = 0
    for _ in range(reps):
        t0 = time.perf_counter()
        d = _post(base + "/embeddings", {"model": "bench", "input": payloads,
                                         "encoding_format": "float"}, timeout=300)
        dt = time.perf_counter() - t0
        lat.append(dt)
        dims = len(d["data"][0]["embedding"])
    out.update(ok=True, docs_per_s=round(batch / statistics.mean(lat), 2),
               p50_s=round(statistics.median(lat), 4),
               p95_s=round(sorted(lat)[max(0, int(0.95 * len(lat)) - 1)], 4),
               dims=dims)
    return out


# --------------------------------------------------------------------- agent arm

def agent_bench(base: str, prompt: str, max_tokens: int, embeds_during: dict | None) -> dict:
    """Fixed prompt, fixed completion; prefill_ms/decode_ms come from the server's
    own timing block. `embeds_during` = {label, endpoint}: the embedding stream runs
    concurrently, and THAT pair (agent speed, embedding stream on) is the parity
    measurement — the shared LPDDR5X is the resource under test."""
    base = base.rstrip("/")
    stop = None
    if embeds_during:
        stop = start_embed_stream(embeds_during["endpoint"])
    try:
        d = _post(base + "/v1/chat/completions", {
            "model": "bench", "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=600)
    finally:
        if stop:
            stop()
    usage = d.get("usage") or {}
    timing = d.get("timings") or {}
    out = {"ok": True,
           "prompt_tokens": usage.get("prompt_tokens", 0),
           "completion_tokens": usage.get("completion_tokens", 0)}
    if timing.get("prefill_ms"):
        out["prefill_tok_s"] = round(out["prompt_tokens"] / (timing["prefill_ms"] / 1000.0), 1)
    if timing.get("decode_ms"):
        out["decode_tok_s"] = round(out["completion_tokens"] / (timing["decode_ms"] / 1000.0), 1)
    return out


def start_embed_stream(base: str):
    """A background process streaming embeddings from `base` until told to stop —
    the load the agent must share the box with. Subprocess (not thread) so the
    GIL can never contaminate the timing of the arm being measured."""
    code = ("import json,sys,time,urllib.request\n"
            "base=sys.argv[1].rstrip('/')\n"
            "texts=['title: none | text: '+t for t in "
            "[\n" + "\n".join(f" {json.dumps(t)}," for t in EMBED_TEXTS) + "\n]]\n"
            "t_end=time.time()+float(sys.argv[2])\n"
            "while time.time()<t_end:\n"
            "    try:\n"
            "        urllib.request.urlopen(urllib.request.Request(base+'/embeddings',"
            "data=json.dumps({'model':'bench','input':texts,'encoding_format':'float'}).encode(),"
            "headers={'content-type':'application/json'}),timeout=120).read()\n"
            "    except Exception:\n"
            "        time.sleep(0.5)\n")
    proc = subprocess.Popen(["python3", "-c", code, base, "120"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return lambda: proc.kill()


# ---------------------------------------------------------------------- driver

def main() -> int:
    ap = argparse.ArgumentParser(description="Parity benchmark: embeddings under the agent, by arm.")
    ap.add_argument("--agent-model", help="gguf for the agent llama-server (GPU)")
    ap.add_argument("--agent-gpu-layers", type=int, default=99)
    ap.add_argument("--agent-port", type=int, default=8393)
    ap.add_argument("--embed-endpoint", action="append", default=[],
                    help="LABEL=URL, repeatable: one arm per endpoint")
    ap.add_argument("--batch", type=int, default=32, help="texts per embedding request")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=192)
    ap.add_argument("--skip-agent", action="store_true", help="embedding arms only")
    args = ap.parse_args()

    arms = []
    for spec in args.embed_endpoint:
        label, _, url = spec.partition("=")
        arms.append((label, url))
    if not arms:
        print("no --embed-endpoint arms given; nothing to compare", file=sys.stderr)
        return 2

    out_dir = pathlib.Path.home() / "ai" / "logs" / (
        "npu-parity-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"arms": [], "agent": None, "agent_under_load": {}}

    for label, url in arms:
        res = embed_bench(label, url, args.batch, args.reps)
        report["arms"].append(res)
        print(f"embed[{label}]: " + (json.dumps(res, default=str) if not res.get("ok")
              else f"{res['docs_per_s']} docs/s, p50 {res['p50_s']}s, p95 {res['p95_s']}s"))

    if args.skip_agent:
        print("--skip-agent: agent arms not run")
    elif not args.agent_model:
        print("--agent-model is required for the parity arms (agent idle + under load)",
              file=sys.stderr)
        return 2
    else:
        # Free-RAM refusal (slot_bench discipline): the agent loads here, and a model
        # that does not fit right now must skip, not OOM the box.
        avail = int(open("/proc/meminfo").read().split("MemAvailable:")[1].split()[0]) // 1024
        import os
        need_mb = int(os.path.getsize(args.agent_model) / (1024 * 1024)) + 8192
        if avail < need_mb:
            print(f"refused: {avail} MiB available < {need_mb} MiB needed for the agent "
                  "(file + KV slack); quiet the box and rerun", file=sys.stderr)
            return 3
        srv = subprocess.Popen(
            ["/home/cortana/ai/releases/llama.cpp-b11669/llama-server",
             "--model", args.agent_model, "--alias", "bench",
             "--host", "127.0.0.1", "--port", str(args.agent_port),
             "--n-gpu-layers", str(args.agent_gpu_layers), "--ctx-size", "16384",
             "--parallel", "1", "--metrics", "--log-disable"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{args.agent_port}"
        try:
            ready = False
            for _ in range(600):
                time.sleep(2)
                try:
                    st, _ = _get(base + "/health", 3)
                    if st == 200:
                        ready = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not ready:
                print("the agent server never answered /health (200); arms skipped",
                      file=sys.stderr)
            else:
                idle = agent_bench(base, PROMPT, args.max_tokens, None)
                report["agent"] = idle
                print(f"agent idle: prefill {idle.get('prefill_tok_s')} tok/s, "
                      f"decode {idle.get('decode_tok_s')} tok/s")
                for label, url in arms:
                    under = agent_bench(base, PROMPT, args.max_tokens,
                                        {"label": label, "endpoint": url})
                    report["agent_under_load"][label] = under
                    print(f"agent + embed[{label}]: prefill {under.get('prefill_tok_s')} tok/s, "
                          f"decode {under.get('decode_tok_s')} tok/s")
        finally:
            srv.terminate()

    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    print(f"report: {out_dir}/report.json")
    return 0



if __name__ == "__main__":
    sys.exit(main())
