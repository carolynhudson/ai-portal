#!/usr/bin/env python3
"""Ad-hoc NPU embedding endpoint for the parity harness (Oct 17 F4).

Serves the compiled EG2 graph (fixed [1,512], vaiml/VitisAI lane) as
/v1/embeddings for tools/npu_parity.py. Not a deployment artifact: parity
verdict is NO-GO (NPU 2.5 vs CPU 31.2 docs/s at S=512); this exists only to
complete the harness's production-length record. Run inside the AMD venv
with /opt/xilinx/xrt/lib FIRST in LD_LIBRARY_PATH (library-shadowing fix,
see ~/ai/logs/NPU-morning-brief.md NIGHT).
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
import tokenizers

_lock = threading.Lock()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8391)
    ap.add_argument("--model", default="/tmp/eg2-dd/preprocessed.onnx")
    ap.add_argument("--vaip-config", required=True)
    ap.add_argument("--tokenizer", default="/home/cortana/ai/models/npu/embeddinggemma-2-src")
    a = ap.parse_args()

    tok = tokenizers.Tokenizer.from_file(a.tokenizer + "/tokenizer.json")
    sess = ort.InferenceSession(a.model, providers=["VitisAIExecutionProvider"],
        provider_options=[{"use_aie": "true", "precision": "aie-mlir",
                           "execution_mode": "vaip-mode", "vaip_config_path": a.vaip_config}])

    def embed(texts):
        outs = []
        with _lock:
            for t in texts:
                ids = tok.encode(t, add_special_tokens=True).ids[:511]
                pad = 512 - len(ids)
                input_ids = np.array([ids + [0] * pad], dtype=np.int64)
                mask = np.array([[1] * len(ids) + [0] * pad], dtype=np.int64)
                v = sess.run(["embed_out"],
                             {"input_ids": input_ids, "attention_mask": mask})
                outs.append(v[0][0].tolist())
        return outs

    class H(BaseHTTPRequestHandler):
        def log_message(self, *p):  # quiet
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "no route"})

        def do_POST(self):
            if self.path != "/v1/embeddings":
                self._send(404, {"error": "no route"})
                return
            n = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(n))
            inp = body.get("input")
            if isinstance(inp, str):
                inp = [inp]
            data = embed(inp)
            self._send(200, {"object": "list", "model": "eg2-npu",
                             "data": [{"object": "embedding", "index": i,
                                       "embedding": v} for i, v in enumerate(data)],
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
