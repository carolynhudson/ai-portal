#!/usr/bin/env python3
"""Embedding-quant quality gate: candidate GGUF vs the reference endpoint.

The sidecar baseline is EmbeddingGemma-2 Q8_0 (measured 111 docs/s CPU).
Smaller quants (Unsloth UD-K_XL dynamic tiers) are only adoptable if they
keep EG2's quality: same sentences in, near-identical vectors out. This
script embeds a fixed probe set (questions, code, long doc, multilingual)
against two /v1/embeddings endpoints and reports per-pair cosine + docs/s.

  tools/embed_quant_gate.py --ref http://127.0.0.1:8390 --cand http://127.0.0.1:8392

Pass rule (owner's parity discipline, applied to quants): every pair cosine
>= 0.99, and report the speed delta so the adoption trade is explicit.
"""
import argparse
import json
import math
import time
import urllib.request

PROBES = [
    "How do I reset my password?",
    "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)",
    ("The Strix Halo iGPU shares system RAM through a GTT aperture; llama.cpp "
     "models live in the aperture, not VRAM, so --models-max is a RAM knob, "
     "and an agent session is evicted the moment a second model loads."),
    "¿Cuántos gigabytes de RAM compartida tiene la máquina?",
    "The quick brown fox jumps over the lazy dog near the riverbank at dawn",
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;",
    "memory recall quality degrades silently when the embedding endpoint is down",
    "Rust borrow checker: &mut T aliasing rules and non-lexical lifetimes",
]


def embed(url: str, model: str, texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/embeddings",
        data=json.dumps({"model": model, "input": texts,
                         "encoding_format": "float"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return [item["embedding"] for item in d["data"]]


def cos(a, b) -> float:
    n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / n if n else 0.0


def rate(url: str, model: str, docs: list[str]) -> float:
    t0 = time.time()
    for rep in range(3):
        half = len(docs) // 2
        embed(url, model, docs[:half])
        embed(url, model, docs[half:])
    return (len(docs) * 3) / (time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="http://127.0.0.1:8390")
    ap.add_argument("--cand", required=True)
    ap.add_argument("--model", default="embeddinggemma2")
    ap.add_argument("--floor", type=float, default=0.99)
    a = ap.parse_args()

    ref = embed(a.ref, a.model, PROBES)
    cand = embed(a.cand, a.model, PROBES)
    cosines = [cos(r, c) for r, c in zip(ref, cand)]
    for i, (p, c) in enumerate(zip(PROBES, cosines)):
        print(f"  cos {c:.5f}  {p[:60]}")
    ok = min(cosines) >= a.floor

    docs = [p for p in PROBES for _ in range(2)]  # 16 docs
    r_ref, r_cand = rate(a.ref, a.model, docs), rate(a.cand, a.model, docs)
    print(f"min cosine {min(cosines):.5f} (floor {a.floor}) -> "
          f"{'PASS' if ok else 'FAIL'}")
    print(f"docs/s  ref {r_ref:.1f}  cand {r_cand:.1f}  ({r_cand / r_ref:.2f}x)")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
