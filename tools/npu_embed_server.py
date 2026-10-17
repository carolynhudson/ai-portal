#!/usr/bin/env python3
"""The NPU embedding endpoint: OpenAI /v1/embeddings in front of an NPU engine.

    tools/npu_embed_server.py --port 8391 --backend stub
    tools/npu_embed_server.py --port 8391 --backend amd-onnx --model-dir ~/ai/models/npu/embeddinggemma-300m

kb_mcp and npu_parity.py speak /v1/embeddings; the CPU sidecar speaks it; this
server speaks it. The parity arms therefore measure the ENGINE, not the wiring —
and when the NPU wins, kb_mcp's PORTAL_EMBED_URL flips to this port and the
code above it does not change. (EmbeddingGemma-300M and EmbeddingGemma 2 share
768 dims and the same task prefixes, so the vectors are interface-compatible;
the quality delta between them is a measured question, not an assumption.)

Backends:
  stub      deterministic keyword vectors — proves the harness end to end
  amd-onnx  AMD's compiled EmbeddingGemma-300M int8 via the Ryzen AI SDK's
            ONNX Runtime (VitisAIExecutionProvider). Requires the SDK venv
            (EULA-gated download, the owner's AMD account); the model dir
            carries AMD's compiled .onnx + .bin/.json meta files.

The shim normalizes every vector before answering: parity ranks must be
comparable across engines, and a provider that stops normalizing silently
skews cosine scores. Stdlib HTTP, no framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 768


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


class StubBackend:
    """Deterministic keyword vectors: 'quant'-ish texts live near x, 'color'-ish
    near y, everything else on the diagonal. Two dims are enough to prove the
    wiring; the harness must not pretend to test quality through a stub."""

    name = "stub"
    dims = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            low = t.lower()
            if "quant" in low:
                out.append([1.0, 0.0])
            elif "color" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.7, 0.7])
        return [_normalize(v) for v in out]


class AmdOnnxBackend:
    """AMD's compiled model under the Ryzen AI ONNX Runtime. Import lives inside
    embed(): starting the server without the SDK venv must fail per-request with
    a clear reason, not at boot with a traceback nobody asked for."""

    name = "amd-onnx"
    dims = DIM

    def __init__(self, model_dir: str):
        import pathlib
        p = pathlib.Path(model_dir).expanduser()
        onnx = sorted(p.glob("*.onnx"))
        if not onnx:
            raise SystemExit(f"no .onnx in {p} — AMD compiled models ship .onnx + .bin/.json meta")
        self.onnx = str(onnx[0])
        self.bin = sorted(str(f) for f in p.glob("*.bin"))
        self.json = sorted(str(f) for f in p.glob("*.json"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        import onnxruntime as ort
        # AMD's compiled-model convention: the .json meta tells the VitisAI EP
        # where the compiled .bin lives (user_meta_config). Kept per-call: the
        # SDK venv's ort is small; a fresh session per batch is fine at memory
        # store volumes and dodges cross-request state we did not ask for.
        opts = ort.SessionOptions()
        meta = self.json[0] if self.json else ""
        providers = [("VitisAIExecutionProvider",
                      {"meta_format": "bin" if self.bin else "json",
                       "user_meta_config": self.bin[0] if self.bin else meta})]
        sess = ort.InferenceSession(self.onnx, sess_options=opts, providers=providers)
        out = []
        for t in texts:
            # Input names vary by compiled export; feed what the graph declares.
            feeds = {}
            for inp in sess.get_inputs():
                if "input_ids" in inp.name:
                    feeds[inp.name] = np.array([_hash_tokens(t, inp.shape)], dtype="int64")
                elif "attention_mask" in inp.name:
                    feeds[inp.name] = np.ones((1, _HASH_LEN), dtype="int64")
            vec = [float(x) for x in sess.run(None, feeds)[0].reshape(-1)]
            if len(vec) != DIM:
                raise RuntimeError(f"compiled model returned {len(vec)} dims, not {DIM}")
            out.append(_normalize(vec))
        return out


_HASH_LEN = 64


def _hash_tokens(text: str, _shape) -> list[int]:
    """Placeholder tokenizer: hash-derived ids. Real tokenization arrives with
    the SDK phase (the model dir ships its tokenizer); a parity run through
    hashed ids measures SPEED faithfully and must be labeled as such for
    quality claims, which belong to kb_mcp's re-rank test, not this shim."""
    h = hashlib.sha256(text.encode()).digest()
    return [101] + [1000 + b for b in h[:_HASH_LEN - 2]] + [102]


class Handler(BaseHTTPRequestHandler):
    backend = None  # set by main()

    def log_message(self, *a):  # quiet: this server sits under a benchmark loop
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, "ok")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/embeddings":
            self._send(404, {"error": "this shim serves /v1/embeddings"})
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n).decode())
            inputs = req.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            vecs = self.backend.embed([str(t) for t in inputs])
            self._send(200, {"object": "list", "model": self.backend.name,
                             "data": [{"object": "embedding", "index": i, "embedding": v}
                                      for i, v in enumerate(vecs)]})
        except Exception as e:  # the parity arm reports the arm as dead, not the shim as crashed
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8391)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--backend", choices=("stub", "amd-onnx"), default="stub")
    ap.add_argument("--model-dir", default="~/ai/models/npu/embeddinggemma-300m")
    args = ap.parse_args()

    Handler.backend = (StubBackend() if args.backend == "stub"
                       else AmdOnnxBackend(args.model_dir))
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"embed shim: {Handler.backend.name} on http://{args.host}:{args.port}/v1/embeddings")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    main()
