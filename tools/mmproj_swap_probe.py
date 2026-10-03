#!/usr/bin/env python3
"""Can a vision projector be borrowed from another quant of the same base model?

Why this is answerable at all: llama.cpp does not match an mmproj to a model by name. `mtmd.cpp`
compares ONE number — `llama_model_n_embd_inp(text)` against `clip_n_mmproj_embd(mmproj)` — and
throws "mismatch between text model (n_embd = X) and mmproj (n_embd = Y)" when they differ
(mtmd.cpp:604). Both numbers sit in the GGUF headers: the LLM's `<arch>.embedding_length` and the
projector's `clip.vision.projection_dim`. So the header predicts the verdict before any worker
starts, and this tool prints that prediction first.

What the header cannot decide, and what the run below settles:

  * the projector type the merger expects (`clip.projector_type`) and the vision special tokens,
    which come from the TOKENIZER, not the projector file;
  * whether the served chat template can actually place image placeholders — a post-train rewrites
    the template, and a rewritten template can drop them while the vocabulary keeps the specials;
  * whether the loaded pair answers about an image.

The image test is functional, not a benchmark: a synthetic PNG with a known colour and shape, one
question, and the answer recorded verbatim. It answers "does vision work", nothing about quality.

The binary is the portal's own llama.cpp build by default — the point is to learn whether THE
PORTAL's runtime does this, not some other fork's. Nothing here writes a config, and the operator's
router on :8080 is never touched (a spare port is picked and the child is waited on).
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import re
import signal
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.core import modelmeta  # noqa: E402

GIB = 1 << 30
BIN_DEFAULT = sorted(glob.glob("/home/cortana/ai/releases/llama.cpp-b*"))[-1] + "/llama-server"


def aperture_used() -> float:
    try:
        return int(pathlib.Path("/sys/class/drm/card1/device/mem_info_gtt_used").read_text().strip()) / GIB
    except OSError:
        return -1.0


def embedding_length(path: pathlib.Path) -> int | None:
    """The LLM side of mtmd's only numeric gate."""
    meta = modelmeta.sniff_gguf(path) or {}
    for key, val in (meta.get("raw") or {}).items():
        if key.endswith(".embedding_length") and not key.startswith("clip."):
            n = modelmeta._as_int(val)
            if n:
                return n
    return None


def projector_facts(path: pathlib.Path) -> dict:
    raw = (modelmeta.sniff_gguf(path) or {}).get("raw") or {}
    return {
        "projection_dim": modelmeta._as_int(raw.get("clip.vision.projection_dim")),
        "projector_type": raw.get("clip.projector_type"),
        "base_model": raw.get("general.base_model.0.name"),
        "type": raw.get("general.type"),
    }


def vision_specials(path: pathlib.Path) -> list[str]:
    """Special tokens the image splice needs — the vocabulary's own declarations."""
    raw = (modelmeta.sniff_gguf(path) or {}).get("raw") or {}
    toks = raw.get("tokenizer.ggml.tokens") or []
    types = raw.get("tokenizer.ggml.token_type") or []
    out = [t for t, ty in zip(toks, types) if str(ty) == "3"]  # 3 == CONTROL/special
    return [t for t in out if re.search(r"vision|image|media|vl_", str(t), re.I)]


def png(color: tuple[int, int, int], shape: tuple[int, int]) -> bytes:
    """A 128x128 PNG: solid `color` with a white square at the centre."""
    w = h = 128
    x0, y0 = w // 2 - shape[0] // 2, h // 2 - shape[1] // 2
    rows = b""
    for y in range(h):
        row = b"\x00"
        for x in range(w):
            inside = x0 <= x < x0 + shape[0] and y0 <= y < y0 + shape[1]
            row += bytes((255, 255, 255)) if inside else bytes(color)
        rows += row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def post(url: str, body: dict, timeout: int = 180):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Server:
    """Portal build, one slot, a spare port, and a child we always wait on."""

    def __init__(self, binary: str, model: str, mmproj: str, port: int, ctx: int, log: pathlib.Path):
        cmd = [binary, "--host", "127.0.0.1", "--port", str(port), "--model", model,
               "--mmproj", mmproj, "-ngl", "99", "--ctx-size", str(ctx), "--parallel", "1",
               "--jinja", "--no-webui"]
        self.log = log
        self.p = subprocess.Popen(cmd, stdout=open(log, "w"), stderr=subprocess.STDOUT,
                                  start_new_session=True)
        self.base = f"http://127.0.0.1:{port}"
        self.slots = None

    def wait(self, seconds: int = 300) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.p.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return True
            except Exception:
                time.sleep(2)
        return False

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGINT)
            try:
                self.p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="cached model id (repo:quant) or a .gguf path")
    ap.add_argument("--mmproj", required=True, help="projector .gguf path (may come from another repo)")
    ap.add_argument("--binary", default=BIN_DEFAULT)
    ap.add_argument("--port", type=int, default=8155)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--header-only", action="store_true", help="predict from headers, start nothing")
    args = ap.parse_args()

    hub = pathlib.Path.home() / ".cache/huggingface/hub"
    model = pathlib.Path(args.model)
    if not model.is_file():
        org, _, quant = args.model.partition(":")
        folder = hub / f"models--{org.replace('/', '--')}"
        hits = [p for p in glob.glob(str(folder / "snapshots" / "*" / "**" + (f"/*{quant}*.gguf") if quant else "/*.gguf"), recursive=True)
                if "mmproj" not in p.name and "mtp" not in p.name.lower()]
        if not hits:
            print(f"no cached file for {args.model}")
            return 2
        model = pathlib.Path(sorted(hits, key=lambda p: p.stat().st_size)[-1])
    mmproj = pathlib.Path(args.mmproj)
    if not mmproj.is_file():
        print(f"projector not found: {mmproj}")
        return 2

    llm_emb = embedding_length(model)
    proj = projector_facts(mmproj)
    print(f"LLM      : {model.name} ({model.stat().st_size / GIB:.2f} GiB) embedding_length={llm_emb}")
    print(f"projector: {mmproj.name} ({mmproj.stat().st_size / GIB:.2f} GiB) {proj}")
    print(f"vision specials in the LLM vocabulary: {vision_specials(model) or 'NONE'}")
    same = bool(llm_emb) and proj.get("projection_dim") == llm_emb
    print(f"\nmtmd's gate (n_embd_text == clip projection_dim): "
          f"{'MATCH — the loader will accept it' if same else 'MISMATCH — llama.cpp throws at load'}")
    if args.header_only or not same:
        print("header-only verdict recorded; nothing started.")
        return 0 if same else 1

    before = aperture_used()
    print(f"\naperture {before:.2f} GiB used; a load costs file + projector + graph.")
    log = pathlib.Path(f"/tmp/mmproj_swap_{int(time.time())}.log")
    srv = Server(args.binary, str(model), str(mmproj), args.port, args.ctx, log)
    if not srv.wait():
        tail = [l for l in log.read_text(errors="ignore").splitlines()
                if re.search(r"mmproj|mtmd|clip|error|abort|mismatch", l, re.I)][-8:]
        print("server did not become healthy. relevant log lines:")
        for l in tail or [log.read_text(errors="ignore")[-400:]]:
            print("   ", l[:170])
        srv.stop()
        return 1
    print("server healthy.")

    props = {}
    try:
        with urllib.request.urlopen(f"{srv.base}/props", timeout=10) as r:
            props = json.loads(r.read())
    except Exception as exc:
        print(f"props: {exc}")
    caps = {k: v for k, v in props.items() if "vision" in k.lower() or "mmproj" in k.lower()
            or "support" in k.lower()}
    print(f"/props vision-related keys: {caps or '(none reported)'}")

    img = png((200, 30, 30), (60, 60))
    import base64
    q = post(f"{srv.base}/v1/chat/completions", {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "One short answer: what colour is the background of this image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img).decode()}}]}],
        "max_tokens": 40, "temperature": 0.0})
    answer = (q.get("choices") or [{}])[0].get("message", {}).get("content")
    print(f"image question answer: {str(answer)[:200]!r}")
    ok = bool(answer) and "red" in str(answer).lower()
    print(f"vision through a borrowed projector: {'WORKS' if ok else 'DID NOT ANSWER AS EXPECTED'}")

    srv.stop()
    print(f"aperture back to {aperture_used():.2f} GiB (peak during run "
          f"{max(before, aperture_used()):.2f} measured at exit; log {log})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
