#!/usr/bin/env python3
"""EG2 text-path ONNX export for the RyzenAI NLP (BF16) compiler.

Path F (docs/PLAN.md §4a F1/F2): the EG2 text trunk — 24L/512h with PLE,
no lm_head, 768-d output — exported as a FIXED-shape graph (embedding =
single forward pass, no KV cache; fixed shapes are what the DD flow fixes
anyway). Mean-pool + L2-normalize are baked into the graph so the compiled
artifact is an embedding endpoint, not a hidden-state dump.

Quality anchor (owner's discipline — no smuggling): the exported graph must
reproduce the live Q8_0 sidecar's embeddings (cos >= 0.99 on the probe set
via --ref-url) BEFORE any parity claim. The bf16 weights are cast to fp32
for export; the DD strategy's initializers_to_bfloat16 pass handles the
compiler-side dtype.

Run in the export venv (~ai/venvs/ryzenai-e; transformers>=5 knows the
embedding_gemma2 arch; the AMD compiler venv ryzenai-b consumes the .onnx):

  ~/ai/venvs/ryzenai-e/bin/python tools/eg2_export_onnx.py \
      --model ~/ai/models/npu/embeddinggemma-2-src \
      --out /tmp/eg2-text.onnx --seq 512 \
      --ref-url http://127.0.0.1:8390
"""
import argparse
import json
import urllib.request

import numpy as np
import onnx
import torch
from transformers import AutoModel, AutoTokenizer

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


class TextEmbedding(torch.nn.Module):
    """text forward + mean pool + L2 normalize (the sentence-transformers
    head: 1_Pooling MEAN + 2_Normalize, from modules.json)."""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        h = self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        m = attention_mask.unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1)
        return torch.nn.functional.normalize(pooled, dim=-1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="/home/cortana/ai/models/npu/embeddinggemma-2-src")
    ap.add_argument("--out", default="/tmp/eg2-text.onnx")
    ap.add_argument("--seq", type=int, default=512)
    ap.add_argument("--ref-url", default="", help="sidecar /v1/embeddings base for the quality anchor")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(a.model)
    m = AutoModel.from_pretrained(a.model, dtype=torch.float32).eval()
    net = TextEmbedding(m)

    texts = [t for t in PROBES]
    ids = tok(texts, padding="max_length", max_length=a.seq,
              return_tensors="pt")
    print("exporting fixed-shape graph: B=1 S=%d -> [1,768]" % a.seq)
    torch.onnx.export(
        net, (ids["input_ids"][:1], ids["attention_mask"][:1]), a.out,
        input_names=["input_ids", "attention_mask"], output_names=["embed_out"],
        dynamic_axes={}, opset_version=18,
        do_constant_folding=True,
    )
    model = onnx.load(a.out, load_external_data=False)
    model.ir_version = 10  # AMD ort caps at IR 13; onnx 1.23 writes 14
    onnx.save(model, a.out)
    import os
    print(f"wrote {a.out}: {os.path.getsize(a.out)/1e6:.1f} MB graph (+ .data)")

    # CPU-run sanity + quality anchor vs the live sidecar
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 8
    sess = ort.InferenceSession(a.out, sess_options=so, providers=["CPUExecutionProvider"])
    outs = []
    for t in texts:
        e = tok([t], padding="max_length", max_length=a.seq, return_tensors="pt")
        outs.append(sess.run(["embed_out"],
                             {"input_ids": e["input_ids"].numpy(),
                              "attention_mask": e["attention_mask"].numpy()})[0][0])
    tg = np.stack(outs)
    assert np.isfinite(tg).all(), "non-finite embeddings from the exported graph"
    if a.ref_url:
        req = urllib.request.Request(a.ref_url.rstrip("/") + "/v1/embeddings",
            data=json.dumps({"model": "embeddinggemma2", "input": texts,
                             "encoding_format": "float"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            ref = np.array([d["embedding"] for d in json.load(r)["data"]])
        cs = [float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))
              for x, y in zip(tg, ref)]
        for p, c in zip(texts, cs):
            print(f"cos {c:.5f}  {p[:58]}")
        print("min cosine vs sidecar:", round(min(cs), 5),
              "PASS" if min(cs) >= 0.99 else "FAIL")


if __name__ == "__main__":
    main()
