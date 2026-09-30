#!/usr/bin/env python3
"""Measure the numbers that actually decide a model's slot on this box.

    .venv/bin/python tools/slot_bench.py --model repo:QUANT [--model ...] \
                                        [--download] [--role fim|chat|agent]

Run it with the portal UI closed. It starts its OWN llama-server per candidate on
a spare port, so it never touches the running router, presets.ini, or the portal
database. It reads nothing outside the Hugging Face cache and writes only
~/ai/logs/slot-bench-<UTC timestamp>/.

Why a script instead of inline probes: every number below needs the box to be
quiescent, and a model has to be resident to get it. The three things that decide
a slot are not on any model card:

  prefill tok/s at editor/RAG-sized prompts   -- how long a keystroke burst blocks
  decode tok/s                                -- how fast an answer appears
  peak GTT aperture + peak RSS                -- whether it fits beside the next model

It also refuses, on purpose, in three situations:

  * not enough aperture/RAM free for THIS model and its KV pool  -> skipped, not OOM'd
  * a "coder" candidate whose vocab has no FIM tokens            -> flagged, /infill is dead
  * an abliterated / heretic / uncensored repo in the coding set  -> refused

FIM token presence and mmproj pairing are read from the GGUF header with the
portal's own reader, so the script and the portal cannot disagree about a file.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from app.core import modelmeta  # the portal's own GGUF header reader  # noqa: E402

GIB = 1 << 30

# ---------------------------------------------------------------- the ceilings
#
# The aperture (GTG/GTT window) is the hard wall on a Strix Halo: weights, KV and
# scratch all have to be addressable inside it, and it is smaller than RAM.
# Measured on this box: aperture 108.00 GiB, RAM 122.83 GiB. The unpriced extras
# are llama.cpp's own defaults, not my invention -- a prompt cache of 8192 MiB
# and 32 context checkpoints per slot (see docs/PORTAL_PLAN.md 6.12.6).
APERTURE_SLACK = 6 * GIB        # KV pool + scratch + checkpoint buffers
MEM_SLACK = 10 * GIB            # page cache churn during load, plus the portal

LOW_SAFETY = re.compile(r"abliterat|heretic|uncensored|de-?restrict|nsfw|roleplay", re.I)
AUX = re.compile(r"(mmproj|mtp-|dflash-|eagle3-|dspark-)", re.I)


def aperture() -> tuple[int, int]:
    base = Path("/sys/class/drm/card1/device")
    try:
        return (int((base / "mem_info_gtt_used").read_text()),
                int((base / "mem_info_gtt_total").read_text()))
    except OSError:
        return 0, 0


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def free_ports(n: int) -> list[int]:
    out = []
    for port in range(8130, 8230):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                out.append(port)
            except OSError:
                continue
        if len(out) == n:
            break
    return out


# --------------------------------------------------------------- finding files
def hub_files(repo: str) -> list[dict]:
    """Name + bytes for every GGUF in a Hub repo, via the portal's token."""
    from huggingface_hub import HfApi
    tok = Path.home().joinpath(".cache/huggingface/token")
    api = HfApi(token=tok.read_text().strip()) if tok.exists() else HfApi()
    info = api.model_info(repo, files_metadata=True)
    return [{"path": s.rfilename, "bytes": s.size or 0}
            for s in (info.siblings or []) if s.rfilename.endswith(".gguf")]


def cached_paths(repo: str) -> dict[str, Path]:
    """What llama.cpp can already see in the HF cache, keyed by QUANT tag.

    Mirrors the runtime's rule (common/download.cpp): part 1 of a split is the
    model, mmproj/mtp/dflash/eagle3/dspark are not.
    """
    hub = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache/huggingface/hub"))
    folder = hub / f"models--{repo.replace('/', '--')}"
    refs = folder / "refs"
    revs = []
    if refs.is_dir():
        for r in sorted(refs.iterdir()):
            try:
                revs.append(r.read_text().strip())
            except OSError:
                pass
    if not revs:
        revs = [p.name for p in (folder / "snapshots").glob("*")] if (folder / "snapshots").is_dir() else []
    out: dict[str, Path] = {}
    for rev in revs:
        snap = folder / "snapshots" / rev
        if not snap.is_dir():
            continue
        for p in sorted(snap.rglob("*.gguf")):
            name = p.name
            if AUX.search(name):
                continue
            m = re.search(r"(?:[-_.]|^)([A-Z0-9_]{3,}(?:_[A-Z0-9]+){0,3})\.gguf$", name)
            if not m:
                continue
            out.setdefault(m.group(1), p)
    return out


def download(repo: str, quant: str) -> Path | None:
    """Fetch the model (and its projector) into the HF cache llama.cpp reads."""
    from huggingface_hub import hf_hub_download
    want = quant.upper()
    picked = None
    for f in hub_files(repo):
        if AUX.search(f["path"]) or not f["path"].upper().startswith(want.split("-")[0][:2]):
            continue
        if f["path"].upper().replace("-", "_").endswith(f"{want}.GGUF") or want in f["path"].upper():
            picked = f["path"]
            break
    if not picked:
        return None
    p = Path(hf_hub_download(repo, picked))
    for f in hub_files(repo):                      # projector, if it exists
        if "mmproj" in f["path"].lower() and want in f["path"].upper():
            hf_hub_download(repo, f["path"])
    return p


# ------------------------------------------------------------------ the server
class Server:
    """One llama-server on a spare port, in its own process group."""

    def __init__(self, model: Path, port: int, extra: list[str], log: Path):
        self.model, self.port, self.log = model, port, log
        self.extra = extra
        self.proc = None
        self.peak_gtt = 0
        self.peak_rss = 0
        self._stop = threading.Event()

    def _sample(self):
        while not self._stop.is_set():
            used, _ = aperture()
            self.peak_gtt = max(self.peak_gtt, used)
            try:
                for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        self.peak_rss = max(self.peak_rss, int(line.split()[1]) * 1024)
            except OSError:
                pass
            time.sleep(0.5)

    def __enter__(self):
        binary = os.environ.get("AI_LLAMA_SERVER", "/home/cortana/ai/bin/llama-server")
        cmd = [binary, "--model", str(self.model), "--host", "127.0.0.1",
               "--port", str(self.port), "--metrics", "--no-webui"] + self.extra
        self.proc = subprocess.Popen(cmd, start_new_session=True,
                                     stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        self._thr = threading.Thread(target=self._sample, daemon=True)
        self._thr.start()
        deadline = time.time() + 600
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited {self.proc.returncode}; see {self.log}")
            try:
                if json.loads(self.get("/health", timeout=5)).get("status") == "ok":
                    return self
            except Exception:
                time.sleep(1.0)
        raise TimeoutError(f"health never came up; see {self.log}")

    def __exit__(self, *exc):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            pgid = os.getpgid(self.proc.pid)
            for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 10)):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                for _ in range(wait * 2):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.5)
                if self.proc.poll() is not None:
                    break
        used, total = aperture()
        print(f"      aperture back to {used/GIB:.2f} / {total/GIB:.2f} GiB"
              f"  (peak during run {self.peak_gtt/GIB:.2f} GiB)")

    def get(self, path: str, timeout: int = 60):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}",
                                    timeout=timeout) as r:
            return r.read().decode()

    def post(self, path: str, body: dict, timeout: int = 300):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                    data=json.dumps(body).encode(),
                                    headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw), time.perf_counter() - t0


def counters(srv: Server) -> dict[str, float]:
    """llama.cpp's own accounting, when --metrics is on."""
    try:
        txt = srv.get("/metrics", timeout=10)
    except Exception:
        return {}
    want = ("llama:prompt_seconds", "llama:prompt_tokens",
            "llama:decode_seconds", "llama:decode_tokens")
    out = {}
    for line in txt.splitlines():
        name = line.split("{", 1)[0].split()[0] if line and not line.startswith("#") else ""
        if name in want:
            out[name] = float(line.rsplit(None, 1)[1])
    return out


def deltas(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {k: b.get(k, 0.0) - a.get(k, 0.0) for k in set(a) | set(b)}


# ------------------------------------------------------------------- the tests
FIM_SNIPPETS = {                       # the operator's own language list
    "python":     ("def parse_gguf(path):\n    header = ", "\n    return header"),
    "c++":        ("std::vector<uint8_t> read_bounded(std::istream& in, size_t n) {\n    ", "\n}"),
    "c#":         ("public static string Slugify(string name)\n{\n    ", "\n}"),
    "vb":         ("Public Function NormalizePath(ByVal p As String) As String\n    ", "\nEnd Function"),
    "node.js":    ("async function fetchJson(url) {\n  ", "\n}"),
    "sql":        ("SELECT m.id, m.bytes FROM model_index m\nWHERE m.source = 'cache'\n  ", "\nORDER BY m.bytes DESC;"),
    "typescript": ("export function budget(rows: Row[]): number {\n  ", "\n}"),
    "lua":        ("function clamp(x, lo, hi)\n  ", "\nend"),
    "ruby":       ("def shard_suffix(name)\n  ", "\nend"),
    "bash":       ("gtt_used() {\n  ", "\n}"),
    "rust":       ("fn quant_from_name(name: &str) -> Option<&str> {\n    ", "\n}"),
}


def bench_fim(srv: Server, report: dict) -> None:
    """/infill with an editor-shaped prefix+suffix, then 4 at once.

    An IDE does not send one request; it sends a burst and needs the last one to
    win. Parallel slots and per-request latency both matter, so both are measured.
    """
    if "fim" not in json.dumps(report.get("vocab", {})).lower():
        report["infill"] = {"skipped": "no FIM tokens in the vocab"}
        return
    probe, _ = srv.post("/infill", {"prompt": "def f():\n    return", "suffix": "",
                                    "n_predict": 8, "temperature": 0.0, "cache": False},
                        timeout=120)
    if "content" not in probe:
        report["infill"] = {"error": "/infill returned no content",
                            "keys": sorted(probe)[:8]}
        return

    lat = []
    for lang, (pre, suf) in FIM_SNIPPETS.items():
        body, dt = srv.post("/infill", {"prompt": pre, "suffix": suf, "n_predict": 64,
                                        "temperature": 0.0, "cache": False}, timeout=180)
        got = (body.get("content") or "").strip()
        tok = body.get("tokens") or []
        lat.append({"lang": lang, "s": round(dt, 3),
                    "tok_per_s": round(len(tok) / dt, 1) if dt else None,
                    "completion": got[:120].replace("\n", "\\n")})
        print(f"      {lang:<11} {dt*1000:6.0f} ms  "
              f"{(len(tok)/dt if dt else 0):6.1f} tok/s  {got[:60]!r}")
    report["infill_samples"] = lat

    burst = FIM_SNIPPETS["python"][0], FIM_SNIPPETS["rust"][0], \
            FIM_SNIPPETS["typescript"][0], FIM_SNIPPETS["bash"][0]
    def one(prefix):
        _, dt = srv.post("/infill", {"prompt": prefix, "suffix": "", "n_predict": 64,
                                     "temperature": 0.0, "cache": False}, timeout=180)
        return dt
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        times = list(pool.map(one, burst))
    report["infill_burst4"] = {"per_request_s": [round(t, 3) for t in times],
                               "p95_s": round(statistics.quantiles(times, n=20)[18], 3)
                               if len(times) > 1 else round(max(times), 3),
                               "wall_s": round(max(times), 3)}
    print(f"      4 concurrent /infill: {[round(t,2) for t in times]} s")


def bench_speed(srv: Server, report: dict) -> None:
    """Prefill at editor/RAG sizes, decode of a short answer, using llama.cpp's counters."""
    out = {}
    for pp, tg in ((512, 64), (2048, 64), (8192, 64)):
        payload = "Describe what this code does. " + "struct S { int x; };\n" * (pp // 8)
        before = counters(srv)
        body, wall = srv.post("/completion", {"prompt": payload, "n_predict": tg,
                                              "temperature": 0.0, "cache": False},
                              timeout=600)
        d = deltas(before, counters(srv))
        ps, ds = d.get("llama:prompt_seconds", 0.0), d.get("llama:decode_seconds", 0.0)
        pt, dt_ = d.get("llama:prompt_tokens", 0.0), d.get("llama:decode_tokens", 0.0)
        out[f"{pp}+{tg}"] = {
            "wall_s": round(wall, 3),
            "ttft_s": round(ps, 3) if ps else None,
            "prefill_tok_s": round(pt / ps, 1) if ps and pt else None,
            "decode_tok_s": round(dt_ / ds, 1) if ds and dt_ else None,
            "usage": body.get("usage") or {k: body.get(k) for k in
                                           ("prompt_tokens", "completion_tokens") if k in body},
        }
        r = out[f"{pp}+{tg}"]
        print(f"      pp{pp:<5} tg{tg:<4} TTFT {str(r['ttft_s']):>7}s   "
              f"prefill {str(r['prefill_tok_s']):>8} tok/s   decode {str(r['decode_tok_s']):>7} tok/s")
    report["speed"] = out


def bench_tools(srv: Server, report: dict) -> None:
    """Can it actually emit a schema-valid tool call? Cheap and decisive."""
    body, dt = srv.post("/chat/completions", {
        "messages": [{"role": "user",
                      "content": "What is the GTT aperture used right now? Use a tool."}],
        "tools": [{"type": "function", "function": {
            "name": "read_aperture",
            "description": "Read the amdgpu GTT aperture usage counters.",
            "parameters": {"type": "object",
                           "properties": {"unit": {"type": "string",
                                                   "enum": ["bytes", "gib"]}},
                           "required": ["unit"]}}}],
        "tool_choice": "auto", "temperature": 0.0, "max_tokens": 200}, timeout=240)
    try:
        call = body["choices"][0]["message"]["tool_calls"] or []
    except KeyError:
        call = []
    ok = bool(call) and isinstance(call[0].get("function", {}).get("name"), str)
    args_ok = False
    if ok:
        try:
            args = json.loads(call[0]["function"]["arguments"])
            args_ok = "unit" in args
        except (json.JSONDecodeError, TypeError):
            pass
    report["tool_call"] = {"emitted": ok, "args_valid_json_with_required_key": args_ok,
                           "raw": (call[:1] or [{}])[0].get("function", {}),
                           "s": round(dt, 2)}
    print(f"      tool call emitted={ok} args_ok={args_ok}")


# GGUF v3 value types (gguf.h): 0 UINT8 1 INT8 2 UINT16 3 INT16 4 UINT32 5 INT32
# 6 FLOAT32 7 BOOL 8 STRING 9 ARRAY 10 UINT64 11 INT64 12 FLOAT64.
_SC = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
       10: "<Q", 11: "<q", 12: "<d"}
_SC_SZ = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _scan_vocab(path: Path, cap: int = 64 << 20) -> tuple[list, list]:
    """tokenizer.ggml.tokens / .added_tokens straight out of the header.

    modelmeta.sniff_file keeps preview strings, not whole arrays, and "does this
    file's vocab carry FIM tokens" is the one fact the autocomplete slot hinges
    on -- so it is read here rather than summarised away.
    """
    with path.open("rb") as f:
        buf = f.read(cap)
    if buf[:4] != b"GGUF":
        return [], []
    import io
    f = io.BytesIO(buf)
    f.read(4); f.read(4); f.read(8)
    nkv = int.from_bytes(f.read(8), "little")

    def rstr():
        n = int.from_bytes(f.read(8), "little")
        b = f.read(n)
        if len(b) < n:
            raise EOFError
        return b.decode("utf-8", "replace")

    toks, added = [], []
    try:
        for _ in range(nkv):
            key = rstr()
            t = int.from_bytes(f.read(4), "little")
            if t == 8:
                rstr()
            elif t in _SC:
                f.read(_SC_SZ[t])
            elif t == 9:
                et = int.from_bytes(f.read(4), "little")
                n = int.from_bytes(f.read(8), "little")
                if et == 8:
                    vals = [rstr() for _ in range(n)]
                    if key.endswith("tokenizer.ggml.tokens"):
                        toks = vals
                    elif key.endswith("tokenizer.ggml.added_tokens"):
                        added = vals
                elif et in _SC:
                    f.read(n * _SC_SZ[et])
                else:
                    break
            else:
                break
    except (EOFError, ValueError):
        pass                                    # header exceeded `cap`
    return toks, added


def header_facts(model: Path) -> dict:
    """The facts the file itself states, plus whether /infill can work at all."""
    meta = modelmeta.sniff_file(model) or {}
    toks, added = _scan_vocab(model)
    fim = sorted({t for t in list(toks) + list(added)
                  if isinstance(t, str) and "fim" in t.lower() and t.startswith("<")})
    return {"architecture": meta.get("arch"),
            "declared_type": meta.get("declared_type"),
            "trained_ctx": meta.get("context_length"),
            "sliding_window": meta.get("sliding_window"),
            "is_moe": meta.get("is_moe"),
            "experts": meta.get("expert_count"),
            "experts_used": meta.get("expert_used_count"),
            "has_chat_template": meta.get("has_chat_template"),
            "vocab_size": len(toks),
            "fim_tokens": fim,
            "vocab": {"fim": bool(fim)}}


# ---------------------------------------------------------------------- driver
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", default=[],
                    help="repo[:QUANT], repeatable. e.g. IFM/K2-Horizon-MoVA-36B-A4B-GGUF:Q4_K_M")
    ap.add_argument("--role", choices=["fim", "chat", "agent", "embed"], default="chat",
                    help="which extra tests to run for every model in this invocation")
    ap.add_argument("--download", action="store_true",
                    help="fetch missing models into the HF cache llama.cpp reads")
    ap.add_argument("--ctx", type=int, default=8192, help="context to reserve per slot")
    ap.add_argument("--parallel", type=int, default=4, help="server slots (burst handling)")
    ap.add_argument("--quant", default="", help="fallback quant when the id has none")
    ap.add_argument("--dry-run", action="store_true", help="size and guard only; load nothing")
    args = ap.parse_args()

    if not args.model:
        ap.error("give at least one --model repo[:QUANT]")

    out_dir = Path.home() / "ai/logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_path = out_dir / f"slot-bench-{stamp}.json"
    log_dir = out_dir / f"slot-bench-{stamp}.logs"
    log_dir.mkdir(exist_ok=True)

    results = []
    for spec in args.model:
        repo, _, quant = spec.partition(":")
        quant = (quant or args.quant or "").upper()
        rec: dict = {"repo": repo, "quant": quant, "role": args.role}
        print(f"\n=== {repo}" + (f":{quant}" if quant else ""))

        if LOW_SAFETY.search(repo) and args.role in ("fim", "agent"):
            print("    REFUSED: a low-safety requant is not admissible for coding/agentic")
            rec["refused"] = "low-safety requant in a coding/agentic slot"
            results.append(rec)
            continue

        if not quant:
            avail = sorted(cached_paths(repo))
            print(f"    no :QUANT given; cached quants: {avail or 'none'}")
            results.append(rec)
            continue

        path = cached_paths(repo).get(quant)
        if path is None and args.download:
            print(f"    not cached -- downloading {quant}")
            download(repo, quant)
            path = cached_paths(repo).get(quant)
        if path is None:
            hub = [f["path"] for f in hub_files(repo) if quant in f["path"].upper()]
            print(f"    not cached. Hub files matching {quant}: {hub[:4] or 'none'}")
            rec["missing"] = True
            results.append(rec)
            continue

        size = path.stat().st_size
        rec["file"] = str(path)
        rec["file_gib"] = round(size / GIB, 2)
        rec.update(header_facts(path))
        mm = sorted(p for p in path.parent.rglob("*.gguf") if "mmproj" in p.name.lower())
        mm_bytes = sum(p.stat().st_size for p in mm)
        rec["mmproj_gib"] = round(mm_bytes / GIB, 2)

        # ---- the guard: aperture first, because on this box it is the wall ----
        used, total = aperture()
        need = size + mm_bytes + args.ctx * 4096      # KV+scratch estimate, loose
        free_ap = total - used
        mem = mem_available()
        moe = (f"MoE {rec['experts']} experts / {rec['experts_used']} active"
               if rec.get("is_moe") else "dense")
        print(f"    file {size/GIB:6.2f} GiB  {moe}  trained_ctx {rec.get('trained_ctx')}  "
              f"sw {rec.get('sliding_window')}  FIM tokens "
              f"{'yes' if rec['fim_tokens'] else 'NO'}")
        print(f"    aperture {used/GIB:.2f}/{total/GIB:.2f} GiB used, free {free_ap/GIB:.2f}; "
              f"needs ~{need/GIB:.2f};  MemAvailable {mem/GIB:.2f} GiB")
        rec.update({"gtt_used_gib": round(used / GIB, 2), "gtt_total_gib": round(total / GIB, 2),
                    "gtt_free_gib": round(free_ap / GIB, 2),
                    "mem_available_gib": round(mem / GIB, 2), "need_gib": round(need / GIB, 2)})
        if args.dry_run:
            print("    dry run: not loading")
            results.append(rec)
            continue
        if need + APERTURE_SLACK > free_ap:
            print(f"    SKIPPED: would exceed the aperture (need {need/GIB:.1f} + "
                  f"{APERTURE_SLACK/GIB:.0f} slack > {free_ap/GIB:.1f} free)")
            rec["skipped"] = "aperture"
            results.append(rec)
            continue
        if size + mm_bytes + MEM_SLACK > mem:
            print(f"    SKIPPED: MemAvailable {mem/GIB:.1f} GiB too small to load it")
            rec["skipped"] = "ram"
            results.append(rec)
            continue

        port = free_ports(1)
        if not port:
            print("    SKIPPED: no spare port")
            rec["skipped"] = "port"
            results.append(rec)
            continue

        extra = ["--ctx-size", str(args.ctx), "--parallel", str(args.parallel),
                 "--n-gpu-layers", "99", "--flash-attn", "on"]
        if mm:
            extra += ["--mmproj", str(mm[0])]
        try:
            with Server(path, port[0], extra, log_dir / f"{repo.split('/')[-1]}.log") as srv:
                props = json.loads(srv.get("/props", timeout=20))
                rec["served"] = {"n_ctx": props.get("n_ctx"),
                                 "n_ctx_train": props.get("n_ctx_train"),
                                 "total_n_ctx": props.get("total_n_ctx"),
                                 "slot_n_ctx": props.get("slot_n_ctx"),
                                 "use_mlock": props.get("use_mlock")}
                print(f"    served n_ctx={props.get('n_ctx')} train={props.get('n_ctx_train')} "
                      f"slots={args.parallel} total_n_ctx={props.get('total_n_ctx')}")
                bench_speed(srv, rec)
                if args.role in ("fim", "chat", "agent"):
                    bench_fim(srv, rec)
                if args.role == "agent":
                    bench_tools(srv, rec)
                rec["peak_gtt_gib"] = round(srv.peak_gtt / GIB, 2)
                rec["peak_rss_gib"] = round(srv.peak_rss / GIB, 2)
                print(f"    peak aperture {rec['peak_gtt_gib']:.2f} GiB, peak RSS "
                      f"{rec['peak_rss_gib']:.2f} GiB")
        except Exception as e:                                # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
            print(f"    ERROR {rec['error']}")
        finally:
            time.sleep(4)                                     # let the driver reclaim
        results.append(rec)
        report_path.write_text(json.dumps(results, indent=1))

    report_path.write_text(json.dumps(results, indent=1))
    print(f"\nreport: {report_path}\nlogs:   {log_dir}")
    used, total = aperture()
    print(f"final aperture: {used/GIB:.2f} / {total/GIB:.2f} GiB")
    stragglers = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True,
                                text=True).stdout.split()
    print(f"llama-server processes left: {stragglers or 'none'} "
          f"(the router's own will be listed -- do not kill it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
