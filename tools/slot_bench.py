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
import mmap
import os
import re
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from app.core import modelindex, modelmeta   # the portal's own readers  # noqa: E402

GIB = 1 << 30
SCRATCH_BYTES = GIB          # compute graphs + buffers; measured ~1 GiB overhead
#: llama-server's own --cache-ram default is 8192 MiB per worker, and the prompt
#: cache plus shard page-in is what actually empties MemAvailable (6.12.8).
CACHE_RAM_BYTES = 8 * GIB

# ---------------------------------------------------------------- the ceilings
#
# The aperture (GTG/GTT window) is the hard wall on a Strix Halo: weights, KV and
# scratch all have to be addressable inside it, and it is smaller than RAM.
# Measured on this box: aperture 108.00 GiB, RAM 122.83 GiB. The unpriced extras
# are llama.cpp's own defaults, not my invention -- a prompt cache of 8192 MiB
# and 32 context checkpoints per slot (see docs/PORTAL_PLAN.md 6.12.6).
APERTURE_SLACK = 4 * GIB        # headroom beyond the priced KV/scratch
MEM_SLACK = 10 * GIB            # page cache churn during load, plus the portal

LOW_SAFETY = re.compile(r"abliterat|heretic|uncensored|de-?restrict|nsfw|roleplay", re.I)
# Not a model, per the runtime's own rule — and the rule is case-sensitive. The
# portal pins it against `llama-server --cache-list` (test_modelindex):
# `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` is a loadable model, while `mtp-foo.gguf` is
# a sidecar. This file used to keep its own list matched with re.I, which hid the
# first and bench-qualified the second.
AUX = re.compile("|".join(re.escape(m) for m in modelindex.NOT_A_MODEL_MARKERS))


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


#: What a file or id has to contain to be a draft head rather than a model. Same
#: markers llama.cpp skips when it lists the cache, applied here to refuse a probe
#: that would otherwise hang (see the guard in main()).
_DRAFT_MARKERS = tuple(m for m in modelindex.NOT_A_MODEL_MARKERS if m != "mmproj")


def _draft_like(name: str) -> bool:
    """Case-sensitive on purpose — see AUX. Lower-casing here meant the Qwopus
    MTP model was refused as if it were a sidecar."""
    return any(m in str(name) for m in _DRAFT_MARKERS)


def cached_paths(repo: str) -> dict[str, Path]:
    """What llama.cpp can already see in the HF cache, keyed by QUANT tag.

    Asked of the portal's own cache scanner, whose naming rule is pinned against
    `llama-server --cache-list` (test_modelindex). An earlier copy of this
    function parsed file names itself and could not name a **sharded** cache
    model at all: `IQ1_M/repo-IQ1_M-00001-of-00002.gguf` ends in a shard number,
    so the tag lookup missed and a model sitting on disk was reported
    "not cached". Keep the hand parser only as a fallback for when app.core is
    unreachable.
    """
    rows = _cache_rows()
    if rows:
        out: dict[str, Path] = {}
        for r in rows:
            rid = r.get("id") or ""
            if ":" not in rid or not rid.startswith(repo + ":"):
                continue
            p = Path(r.get("path") or "")
            if p.is_file():
                out.setdefault(rid.split(":", 1)[1].upper(), p)
        if out:
            return out
    return _cached_paths_fallback(repo)


_ROWS: list[dict] | None = None


def _cache_rows() -> list[dict]:
    """The portal's cache rows, read once. Never fatal: empty means 'fall back'."""
    global _ROWS
    if _ROWS is None:
        try:
            _ROWS, _problems = modelindex.scan_cache()
        except Exception as e:                                # noqa: BLE001
            print(f"    (cache scanner unavailable: {type(e).__name__}: {e})")
            _ROWS = []
    return _ROWS


def cache_row(repo: str, quant: str) -> dict | None:
    """The index row for repo:QUANT, which knows the **total** across shards.

    `path.stat()` is shard 1 of 2 -- pricing the guard with it under-states a
    split model by about half, which is the kind of error that becomes an OOM
    instead of a refusal.
    """
    want = f"{repo}:{quant}"
    for r in _cache_rows():
        if r.get("id") == want:
            return r
    return None


def _cached_paths_fallback(repo: str) -> dict[str, Path]:
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


def pick_repo_file(files, quant: str) -> str | None:
    """Which hub file a `repo:QUANT` names — ranked, never decided by a name prefix.

    Repos disagree about where the quant tag sits: `IQ3_S/repo-IQ3_S-00001-of-2.gguf`
    (tag dir), `codegemma-2b-Q5_K_M.gguf` (dash suffix) and
    `qwen3-reranker-0.6b-q8_0.gguf` (underscore suffix) all mean the same thing to
    llama.cpp. The old test was `startswith(quant[:2])`, which matched only the tag
    dir, so ggml-org's 300 M embedder "downloaded" nothing and the run reported a
    file that was already on disk as missing.
    """
    want = quant.upper().replace("-", "_")
    scored: list[tuple[int, str]] = []
    for f in files:
        path = f["path"] if isinstance(f, dict) else f
        if AUX.search(path) or not path.lower().endswith(".gguf"):
            continue
        head = path.rsplit("/", 1)[0].upper().replace("-", "_") if "/" in path else ""
        # Compare both ways: authors separate the tag with "-" (codegemma-2b-Q5_K_M)
        # and with "_" (qwen3-reranker-0.6b-q8_0); normalising dashes made the
        # second shape invisible.
        base = path.rsplit("/", 1)[-1].upper()
        names = {base, base.replace("-", "_")}
        if head == want or f"{want}.GGUF" in names:            # Q8_0/x.gguf, Q8_0.gguf
            scored.append((0, path))
        elif any(n.endswith(f"-{want}.GGUF") or n.endswith(f"_{want}.GGUF")
                 for n in names):                             # model-Q8_0[-00001-of-2]
            scored.append((1, path))
        elif any(f"-{want}." in n or f"_{want}." in n or f"-{want}-" in n
                 for n in names):                             # tag anywhere in the name
            scored.append((2, path))
    return sorted(scored)[0][1] if scored else None


def download(repo: str, quant: str) -> Path | None:
    """Fetch the model (its shards and projector too) into the HF cache llama.cpp reads."""
    from huggingface_hub import hf_hub_download
    files = hub_files(repo)
    picked = pick_repo_file(files, quant)
    if picked is None:
        return None
    p = Path(hf_hub_download(repo, picked))
    want = quant.upper().replace("-", "_")
    for f in files:                            # projector, if one exists
        path = f["path"] if isinstance(f, dict) else f
        if "mmproj" in path.lower() and want in path.upper():
            hf_hub_download(repo, path)
    if "-of-" in picked:                       # a split set: shards share a directory
        folder = picked.rsplit("/", 1)[0]
        for f in files:
            path = f["path"] if isinstance(f, dict) else f
            if path.rsplit("/", 1)[0] == folder and path.lower().endswith(".gguf"):
                hf_hub_download(repo, path)
    return p


# ------------------------------------------------------------------ the server
class Server:
    """One llama-server on a spare port, in its own process group."""

    def __init__(self, model: Path | None, port: int, extra: list[str], log: Path,
                 attach: str | None = None, model_id: str | None = None):
        """attach=<base url> benches a router that is *already* running.

        Every request then has to name the model: with a models dir and the hub
        cache in view the router holds several models, and a request without a
        "model" field is refused with 400 "model name is missing from the
        request" -- which is exactly how a whole benchmark run produced nothing.
        """
        self.model, self.port, self.log = model, port, log
        self.extra = extra
        self.attach = attach.rstrip("/") if attach else None
        self.model_id = model_id
        self.proc = None
        self.peak_gtt = 0
        self.peak_rss = 0
        self.min_mem = 1 << 62
        self._stop = threading.Event()

    def _sample(self):
        while not self._stop.is_set():
            used, _ = aperture()
            self.min_mem = min(self.min_mem, mem_available())
            self.peak_gtt = max(self.peak_gtt, used)
            try:
                for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        self.peak_rss = max(self.peak_rss, int(line.split()[1]) * 1024)
            except OSError:
                pass
            time.sleep(0.5)

    def __enter__(self):
        if self.attach:
            json.loads(self.get("/health", timeout=20))
            return self
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
        if self.attach:
            used, total = aperture()
            print(f"      (attached run: nothing was started or stopped; "
                  f"aperture {used/GIB:.2f} / {total/GIB:.2f} GiB, "
                  f"lowest MemAvailable {self.min_mem/GIB:.2f} GiB)")
            return
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
              f"  (peak during run {self.peak_gtt/GIB:.2f} GiB,"
              f" lowest MemAvailable {self.min_mem/GIB:.2f} GiB)")

    def get(self, path: str, timeout: int = 60):
        with urllib.request.urlopen(self._url(path), timeout=timeout) as r:
            return r.read().decode()

    def _url(self, path: str) -> str:
        return (self.attach or f"http://127.0.0.1:{self.port}") + path

    def post(self, path: str, body: dict, timeout: int = 300):
        if self.model_id and "model" not in body:
            body = {**body, "model": self.model_id}
        req = urllib.request.Request(self._url(path),
                                    data=json.dumps(body).encode(),
                                    headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            # "HTTP Error 500" tells the next reader nothing; the server's own
            # message names the missing key or the bad parameter.
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:                                   # noqa: BLE001
                pass
            raise urllib.error.HTTPError(
                e.url, e.code, f"{e.reason} {detail}", e.headers, None) from None
        return json.loads(raw), time.perf_counter() - t0


# b11146 renders counters as llamacpp:<name> and renamed them: the old
# llama:prompt_seconds / llama:decode_seconds simply are not there any more, so
# every rate this script derived came back None. Map both spellings onto one
# canonical key (server-task.cpp:1523 server_task_result_metrics::to_metrics).
_METRICS = {
    "llamacpp:prompt_seconds_total":              "prompt_s",
    "llamacpp:prompt_tokens_total":               "prompt_tok",
    "llamacpp:prompt_tokens_cached_total":        "cached_tok",
    "llamacpp:tokens_predicted_seconds_total":    "decode_s",
    "llamacpp:tokens_predicted_total":            "decode_tok",
    "llamacpp:spec_decode_num_draft_tokens_total":     "draft_tok",
    "llamacpp:spec_decode_num_accepted_tokens_total":  "accepted_tok",
    "llamacpp:spec_decode_num_drafts_total":           "drafts",
    "llama:prompt_seconds":                       "prompt_s",
    "llama:prompt_tokens":                        "prompt_tok",
    "llama:decode_seconds":                       "decode_s",
    "llama:decode_tokens":                        "decode_tok",
}


def counters(srv: Server) -> dict[str, float]:
    """llama.cpp's own accounting, when --metrics is on."""
    try:
        txt = srv.get("/metrics", timeout=10)
    except Exception:
        return {}
    out = {}
    for line in txt.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split()[0]
        key = _METRICS.get(name)
        if key:
            try:
                out[key] = float(line.rsplit(None, 1)[1])
            except ValueError:
                pass
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


def fim_payload(prefix: str, suffix: str, n_predict: int) -> dict:
    """The body /infill accepts — and there is exactly one shape.

    /infill takes input_prefix / input_suffix / input_middle, NOT prompt / suffix:
    the wrong keys are answered 500 "[json.exception.out_of_range.403] key
    'input_suffix' not found" (server-context.cpp:4853). The single-request probe
    was fixed to that; the 4-at-once probe kept the old keys, so every run's
    infill_burst4 row read "fewer than 2 of 4 concurrent requests succeeded" —
    the concurrency question the burst exists to answer went unmeasured while the
    per-language rows looked healthy. One builder, so the three call sites cannot
    disagree again.
    """
    return {"input_prefix": prefix, "input_suffix": suffix, "input_middle": "",
            "n_predict": n_predict, "temperature": 0.0, "cache": False}


def bench_fim(srv: Server, report: dict) -> None:
    """/infill with an editor-shaped prefix+suffix, then 4 at once.

    An IDE does not send one request; it sends a burst and needs the last one to
    win. Parallel slots and per-request latency both matter, so both are measured.
    """
    if "fim" not in json.dumps(report.get("vocab", {})).lower():
        report["infill"] = {"skipped": "no FIM tokens in the vocab"}
        return
    # /infill takes input_prefix / input_suffix / input_middle, NOT prompt /
    # suffix. Sending the latter is answered 500 "key 'input_suffix' not found"
    # (server-context.cpp:4853) -- which is what made every FIM row look dead.
    try:
        probe, _ = srv.post("/infill",
                            fim_payload("def f():\n    return", "", 8),
                            timeout=120)
    except Exception as e:                                    # noqa: BLE001
        # /infill is refused outright when the vocab lacks the FIM triple, and a
        # refusal here is a finding, not a crash.
        report["infill"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"      /infill probe failed: {rec_short(e)}")
        return
    if "content" not in probe:
        report["infill"] = {"error": "/infill returned no content",
                            "keys": sorted(probe)[:8]}
        return

    lat = []
    for lang, (pre, suf) in FIM_SNIPPETS.items():
        try:
            body, dt = srv.post("/infill", fim_payload(pre, suf, 64), timeout=180)
        except Exception as e:                                # noqa: BLE001
            lat.append({"lang": lang, "error": f"{type(e).__name__}: {e}"})
            print(f"      {lang:<11} FAILED {rec_short(e)}")
            continue
        got = (body.get("content") or "").strip()
        tok = body.get("tokens") or []
        t = body.get("timings") or {}
        # /infill answers with the non-OAI completion shape: tokens_predicted +
        # timings (server-task.cpp:339 to_json_non_oaicompat). The "tokens" list
        # is not the count and timings' rate is often absent, which is why every
        # row used to print 0.0 tok/s next to a completion that clearly worked.
        n_pred = body.get("tokens_predicted") or len(tok)
        lat.append({"lang": lang, "s": round(dt, 3), "tok_pred": n_pred,
                    "tok_per_s": round(t["predicted_per_second"], 1)
                    if t.get("predicted_per_second") else
                    (round(n_pred / dt, 1) if dt and n_pred else None),
                    "completion": got[:120].replace("\n", "\\n")})
        print(f"      {lang:<11} {dt*1000:6.0f} ms  "
              f"{(n_pred/dt if dt and n_pred else 0):6.1f} tok/s  {got[:60]!r}")
    report["infill_samples"] = lat

    burst = [FIM_SNIPPETS[k] for k in ("python", "rust", "typescript", "bash")]

    def one(pair):
        prefix, suffix = pair
        try:
            _, dt = srv.post("/infill", fim_payload(prefix, suffix, 64), timeout=180)
            return dt
        except Exception as e:                                # noqa: BLE001
            # silently swallowing this is what hid the payload bug: the count of
            # successes was reported, the reason for the failures was not.
            report.setdefault("infill_burst_errors", []).append(rec_short(e))
            return None
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        times = [t for t in pool.map(one, burst) if t is not None]
    if len(times) < 2:
        report["infill_burst4"] = {"error": "fewer than 2 of 4 concurrent requests succeeded",
                                   "ok": len(times),
                                   "why": (report.get("infill_burst_errors") or [None])[0]}
        return
    report["infill_burst4"] = {"per_request_s": [round(t, 3) for t in times],
                               "p95_s": round(statistics.quantiles(times, n=20)[18], 3)
                               if len(times) > 1 else round(max(times), 3),
                               "wall_s": round(max(times), 3)}
    print(f"      {len(times)}/4 concurrent /infill: {[round(t,2) for t in times]} s")


def rec_short(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:120]


# Measured on this box, 2026-09-30: probing a resident 77.86 GiB model that the
# router had loaded at its full trained context (262144) with an 8192-token prompt
# helped push MemAvailable (already 0.00 GiB) over, and the kernel OOM-killed the
# router service. Attach mode therefore has a floor and small defaults: it may
# read a running box, it may not be what tips it.
ATTACH_MEM_FLOOR = 6 * GIB
ATTACH_PROBES = ((512, 64), (2048, 64))
BIG_PROBES = ((512, 64), (2048, 64), (8192, 64))


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def _timings(body: dict) -> dict:
    """Per-request rates straight out of the response.

    The router on this box runs without --metrics (/metrics -> 400), but every
    completion carries a `timings` object: prompt_ms / prompt_n / predicted_ms /
    predicted_n / cache_n. Prefer the counters when they exist (no HTTP round
    trip in them), fall back to this otherwise.
    """
    t = body.get("timings") or {}
    if not t:
        return {}
    return {"prompt_s": (t.get("prompt_ms") or 0) / 1000.0,
            "prompt_tok": t.get("prompt_n") or 0,
            "decode_s": (t.get("predicted_ms") or 0) / 1000.0,
            "decode_tok": t.get("predicted_n") or 0,
            "cached_tok": t.get("cache_n") or 0}


def bench_speed(srv: Server, report: dict, slot_ctx: int | None,
                probes: tuple = BIG_PROBES) -> None:
    """Prefill at editor/RAG sizes, decode of a short answer, using llama.cpp's counters.

    Two lessons live here. A probe wider than the slot's context is rejected with
    HTTP 400, and one such rejection used to abort the whole model -- so every
    probe is sized against what the server actually serves, and each failure is
    recorded on its own row. Rates come from the runtime's counters rather than
    wall time, because wall time includes the HTTP round trip.
    """
    out = {}
    unhealthy = False
    for pp, tg in probes:
        if unhealthy:
            out[f"{pp}+{tg}"] = {"skipped": "server unhealthy after the previous probe"}
            continue
        if slot_ctx and pp + tg + 64 > slot_ctx:
            out[f"{pp}+{tg}"] = {"skipped": f"needs ~{pp + tg} tokens, slot serves {slot_ctx}"}
            print(f"      pp{pp:<5} tg{tg:<4} skipped: slot context {slot_ctx} too small")
            continue
        payload = "Describe what this code does. " + "struct S { int x; };\n" * (pp // 8)
        before = counters(srv)
        try:
            body, wall = srv.post("/completion", {"prompt": payload, "n_predict": tg,
                                                  "temperature": 0.0, "cache": False},
                                  timeout=600)
        except Exception as e:                                # noqa: BLE001
            # A 500 or a refused connection means the worker is in trouble. Keep
            # hammering it and the remaining numbers are noise -- and on a box at
            # the edge of its memory they are also what tips it over.
            out[f"{pp}+{tg}"] = {"error": rec_short(e)}
            unhealthy = True
            print(f"      pp{pp:<5} tg{tg:<4} FAILED {rec_short(e)} -- stopping speed probes")
        d = deltas(before, counters(srv))
        if not d:
            d, src = _timings(body), "response.timings"
        else:
            src = "metrics"
        ps, ds = d.get("prompt_s", 0.0), d.get("decode_s", 0.0)
        pt, dt_ = d.get("prompt_tok", 0.0), d.get("decode_tok", 0.0)
        row = {"timing_source": src,
            "wall_s": round(wall, 3),
            "ttft_s": round(ps, 3) if ps else None,
            "prefill_tok_s": round(pt / ps, 1) if ps and pt else None,
            "decode_tok_s": round(dt_ / ds, 1) if ds and dt_ else None,
            "prompt_tokens": pt or None,
            "cached_tokens": d.get("cached_tok") or None,
            "usage": body.get("usage") or {k: body.get(k) for k in
                                           ("prompt_tokens", "completion_tokens") if k in body},
        }
        # MTP / draft evidence: only interesting if a draft was wired up.
        if d.get("drafts"):
            row["spec"] = {"drafts": d["drafts"], "draft_tok": d.get("draft_tok"),
                           "accepted_tok": d.get("accepted_tok"),
                           "accept_rate": round((d.get("accepted_tok") or 0) /
                                                d["draft_tok"], 3) if d.get("draft_tok") else None}
        out[f"{pp}+{tg}"] = row
        spec = ""
        if row.get("spec"):
            s = row["spec"]
            spec = (f"   draft {s['draft_tok']}/{s['drafts']} tok, "
                    f"accept {s['accept_rate']}")
        print(f"      pp{pp:<5} tg{tg:<4} TTFT {str(row['ttft_s']):>7}s   "
              f"prefill {str(row['prefill_tok_s']):>8} tok/s   decode {str(row['decode_tok_s']):>7} tok/s"
              + spec)
    report["speed"] = out


SPEED_PROBES = ((512, 64), (2048, 64), (8192, 64))   # editor, RAG chunk, long doc


def bench_tools(srv: Server, report: dict) -> None:
    """Can it actually emit a schema-valid tool call? Cheap and decisive."""
    if not report.get("has_chat_template"):
        report["tool_call"] = {"skipped": "no chat template in the file"}
        print("      tool call skipped: file carries no chat template (base model)")
        return
    try:
        body, dt = _tool_request(srv)
    except Exception as e:                                    # noqa: BLE001
        report["tool_call"] = {"error": rec_short(e)}
        print(f"      tool call FAILED {rec_short(e)}")
        return
    try:
        call = body["choices"][0]["message"]["tool_calls"] or []
    except (KeyError, TypeError, IndexError):
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


def _tool_request(srv: Server):
    return srv.post("/chat/completions", {
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


def find_draft(target: Path) -> Path | None:
    """A draft file that sits beside `target`, or None.

    The target is never its own draft: the Qwopus MTP file matches "mtp" in its
    own name, so an unguarded glob handed the server its own file as -md.
    """
    near = sorted(p for p in target.parent.rglob("*.gguf")
                  if _draft_like(p.name) and p != target
                  and "mmproj" not in p.name.lower())
    return near[0] if near else None


def resolve_draft(spec_draft: str) -> tuple[Path | None, str]:
    """Turn --spec-draft into a file, or explain why it is not one.

    A repo id contains a slash too, so "has a slash" cannot tell a path from an
    id: only an existing file or a .gguf name is a path. Aux files (mmproj, mtp)
    are not indexed as models, so a `repo:MTP` id resolves to nothing — the path
    form is the way to measure one.
    """
    cand = Path(spec_draft).expanduser()
    if cand.is_file() or spec_draft.endswith(".gguf"):
        return (cand, "") if cand.is_file() else (None, "not a file")
    drepo, _, dq = spec_draft.partition(":")
    path = cached_paths(drepo).get((dq or "" ).upper())
    if path:
        return path, ""
    return None, ("neither a file nor a cached model id (aux files like mtp-*.gguf "
                  "are not indexed; pass the path)")


def nextn_tensors(model: Path) -> int | None:
    """How many tensors are named `*.nextn.*` — does the file carry its own head?

    `--spec-type draft-mtp` means two different things depending on the file. A
    model like Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF contains blk.32.nextn.*, so
    the draft lives inside the target and one `--model` is enough. unsloth's
    Qwen3.8-27B puts the same head in a separate MTP/mtp-*.gguf, which llama.cpp
    only picks up through --spec-draft-model. Guessing wrong costs a server that
    refuses to start, so read the tensor names.
    """
    # Keep the file object alive for the whole walk: an mmap whose fd is closed
    # under it raises EBADF on the first read, which used to look like "no head".
    try:
        f = open(model, "rb")
    except OSError:
        return None
    try:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except OSError:
        f.close()
        return None
    try:
        if mm[:4] != b"GGUF":
            return None
        ver, = struct.unpack_from("<I", mm, 4)
        if ver < 2:                       # v1 counts are uint32; nothing here is v1
            return None
        n_tensors, = struct.unpack_from("<Q", mm, 8)
        n_kv, = struct.unpack_from("<Q", mm, 16)
        offs, bo, hit = 24, "<", 0

        def rd_str(o):
            # a GGUF string is a u64 length THEN the bytes; the length is not part
            # of the payload, and slicing from o instead of o + 8 makes every
            # subsequent field read land 8 bytes early.
            n, = struct.unpack_from(bo + "Q", mm, o)
            return mm[o + 8:o + 8 + n].decode("utf-8", "replace"), o + 8 + n

        for _ in range(n_kv):
            _, offs = rd_str(offs)
            t, = struct.unpack_from(bo + "I", mm, offs); offs += 4
            _, offs = modelmeta._gguf_read_value(mm, bo, offs, t)
        for _ in range(n_tensors):
            name, offs = rd_str(offs)
            nd, = struct.unpack_from(bo + "I", mm, offs); offs += 4
            offs += 8 * nd + 4 + 8        # dims, ggml_type, offset
            if ".nextn." in name:
                hit += 1
        return hit
    except Exception:                     # noqa: BLE001 - a header we cannot read
        return None                       # is not worth failing the bench over
    finally:
        mm.close()
        f.close()


def header_facts(model: Path) -> dict:
    """The facts the file itself states, plus whether /infill can work at all."""
    meta = modelmeta.sniff_file(model) or {}
    toks, added = _scan_vocab(model)
    fim = sorted({t for t in list(toks) + list(added)
                  if isinstance(t, str) and "fim" in t.lower() and t.startswith("<")})
    return {"_meta": {k: v for k, v in meta.items() if not isinstance(v, (list, dict))},
            "architecture": meta.get("arch"),
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
def embedding_vector(body) -> list:
    """One input -> one vector, whatever shape the route answered with.

    Measured on b11146: `POST /embedding` returns `[{"index": 0, "embedding":
    [[...768 floats...]]}]` -- a list of per-input dicts whose "embedding" is
    itself nested one level. Reading it as the OpenAI dict raised AttributeError
    ("'list' object has no attribute 'get'") and taking its length reported
    dim=1, which looks like a broken embedder rather than a parsing mistake.
    """
    obj = body
    while isinstance(obj, list) and len(obj) == 1:
        obj = obj[0]
    while isinstance(obj, dict):
        obj = obj.get("embedding") or obj.get("data") or []
        while isinstance(obj, list) and len(obj) == 1:
            obj = obj[0]
    return obj if isinstance(obj, list) else []


def bench_embed(srv: Server, report: dict) -> None:
    """Embedding latency and dimensionality -- what a RAG slot actually costs.

    The router proxies /embeddings (server.cpp:227), so these numbers carry to
    :8080; /rerank is proxied too (server.cpp:229) and is probed here because a
    reranker that cannot rank is worth knowing about before it is downloaded.
    """
    out = {}
    for label, text in (("short", "aperture usage on amdgpu"),
                        ("chunk", " ".join(
                            ["the portal prices a model as weights plus the KV cache at the"
                             " context it reserves, and the runtime adds its own prompt cache"
                             " on top of that"] * 6))):
        try:
            body, dt = srv.post("/embedding", {"input": text}, timeout=180)
            # Shape differs by route: /embedding answers with a bare JSON array
            # (one per input), {"embedding": [...]} in some builds, and
            # /v1/embeddings with data[0].embedding. Assuming the OAI dict is an
            # AttributeError on a list, which reads as "the model cannot embed".
            data = embedding_vector(body)

            shape = (f"list[{len(body)}] of dict " + ",".join(sorted(body[0])[:6])
                     if isinstance(body, list) and body and isinstance(body[0], dict)
                     else type(body).__name__)
            out[label] = {"dim": len(data), "s": round(dt, 4), "shape": shape,
                          "raw": repr(body)[:120]}
            print(f"      embed {label:<6} dim={len(data):<6} {dt*1000:7.1f} ms")
        except Exception as e:                                # noqa: BLE001
            out[label] = {"error": rec_short(e)}
            print(f"      embed {label:<6} FAILED {rec_short(e)}")
    try:
        body, dt = srv.post("/rerank", {"query": "what fits in the aperture",
                                        "documents": ["the GTT window is 108 GiB",
                                                      "codegemma is 1.71 GiB"]},
                            timeout=180)
        res = body.get("results") if isinstance(body, dict) else body
        out["rerank"] = {"results": len(res or []), "s": round(dt, 4)}
        print(f"      /rerank {out['rerank']['results']} results {dt*1000:.1f} ms")
    except Exception as e:                                    # noqa: BLE001
        out["rerank"] = {"error": rec_short(e)}
        print(f"      /rerank FAILED {rec_short(e)} (expected unless the model ranks)")
    report["embedding"] = out


def bench_attached(model_id: str, args, rec: dict) -> None:
    """Same probes, run against a router that is already up and loaded."""
    srv = Server(None, 0, [], Path("/dev/null"), attach=args.attach, model_id=model_id)
    floor = ATTACH_MEM_FLOOR if not args.big_probes else 12 * GIB
    free = mem_available()
    print(f"    MemAvailable {free/GIB:.2f} GiB"
          f"{' -- below the attach floor, refusing to probe' if free < floor else ''}")
    if free < floor:
        rec["refused"] = (f"MemAvailable {free/GIB:.2f} GiB < {floor/GIB:.0f} GiB attach floor "
                          f"(a resident model at full trained context leaves nothing; "
                          f"probing it is what OOM-kills the router)")
        print("    REFUSED. Unload the resident model through the portal, or bench the "
              "model the script loads itself.")
        return
    with srv:
        props = json.loads(srv.get("/props", timeout=30))
        listed = json.loads(srv.get("/models", timeout=30)).get("data") or []
        card = next((m for m in listed if m.get("id") == model_id), None) \
            or next((m for m in listed if m.get("object") == "model"), {})
        cm = card.get("meta") or {}
        slot_ctx = ((props.get("default_generation_settings") or {}).get("n_ctx")
                    or cm.get("n_ctx"))
        rec["model_id"] = card.get("id") or model_id
        rec["served"] = {"slot_n_ctx": slot_ctx,
                         "total_slots": props.get("total_slots"),
                         "total_n_ctx": props.get("total_n_ctx"),
                         "n_ctx_train": cm.get("n_ctx_train"),
                         "n_params": cm.get("n_params"),
                         "served_size_gib": round((cm.get("size") or 0) / GIB, 2),
                         "ftype": props.get("model_ftype"),
                         "vision": (props.get("modalities") or {}).get("vision"),
                         "chat_template_caps": props.get("chat_template_caps")}
        rec["has_chat_template"] = bool(props.get("chat_template_caps"))
        print(f"    attached to {args.attach} as {rec['model_id']}")
        print(f"    served slot_ctx={slot_ctx} slots={props.get('total_slots')} "
              f"weights={rec['served']['served_size_gib']} GiB train={cm.get('n_ctx_train')}")
        if args.role == "embed":
            bench_embed(srv, rec)
            return
        bench_speed(srv, rec, slot_ctx,
                    probes=BIG_PROBES if args.big_probes else ATTACH_PROBES)
        if mem_available() < floor:
            rec["halted"] = f"MemAvailable fell to {mem_available()/GIB:.2f} GiB -- stopping"
            print(f"    HALTED: {rec['halted']}")
            return
        if args.role in ("fim", "chat", "coder", "agent"):
            bench_fim(srv, rec)
        if args.role in ("coder", "agent") and mem_available() >= floor:
            bench_tools(srv, rec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", default=[],
                    help="repo[:QUANT], repeatable. e.g. IFM/K2-Horizon-MoVA-36B-A4B-GGUF:Q4_K_M")
    ap.add_argument("--role", choices=["fim", "chat", "coder", "agent", "embed"],
                    default="chat",
                    help="which extra tests to run for every model in this invocation")
    ap.add_argument("--download", action="store_true",
                    help="fetch missing models into the HF cache llama.cpp reads")
    ap.add_argument("--ctx", type=int, default=16384,
                    help="total context to reserve (a shared KV pool; --kv-unified). "
                         "Was 8192 with --parallel 4, which handed each slot 2048 tokens and made "
                         "the pp2048 probe a guaranteed HTTP 400.")
    ap.add_argument("--parallel", type=int, default=4, help="server slots (burst handling)")
    ap.add_argument("--attach", metavar="URL",
                    help="bench an ALREADY RUNNING router (e.g. http://127.0.0.1:8080): "
                         "nothing is loaded, started or stopped, so it is safe while a "
                         "model is resident")
    ap.add_argument("--name", metavar="ID",
                    help="model id to address in attach mode; default: whatever is loaded")
    ap.add_argument("--spec", choices=["off", "mtp"], default="off",
                    help="speculative decoding (--spec-type draft-mtp). Uses the target's "
                         "own nextn head, or a draft file: see --spec-draft. Required to "
                         "bench a *-MTP-* file at all.")
    ap.add_argument("--spec-draft", metavar="MODEL", default="",
                    help="draft model for --spec mtp: a path or repo:QUANT (--spec-draft-"
                         "model). Default: an mtp-*/draft file found beside the target, "
                         "which is how unsloth ships Qwen3.8-27B's head.")
    ap.add_argument("--big-probes", action="store_true",
                    help="let attach mode also send the 8192-token prefill probe; needs "
                         "12 GiB free instead of 6. Do not use this against a model the "
                         "router loaded at its full trained context.")
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

        if args.attach:
            # Nothing is loaded or stopped: the numbers come from the router the
            # operator is already running, which is the only safe way to measure
            # a 78 GiB model that is already resident.
            rec["attach"] = args.attach
            try:
                bench_attached(args.name or spec, args, rec)
            except Exception as e:                            # noqa: BLE001
                rec["error"] = rec_short(e)
                print(f"    attach failed: {rec['error']}")
            results.append(rec)
            continue

        if LOW_SAFETY.search(repo) and args.role in ("fim", "coder", "agent"):
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

        row = cache_row(repo, quant)
        size = int(row["bytes"]) if row and row.get("bytes") else path.stat().st_size
        rec["file"] = str(path)
        rec["file_gib"] = round(size / GIB, 2)
        rec["shards"] = f"1/{row['shard_total']}" if row and row.get("shard_total") else "1/1"
        if row and int(row.get("shard_total") or 1) > 1:
            rec["file"] += f" (+{int(row['shard_total']) - 1} more shard(s))"
        rec.update(header_facts(path))
        facts = rec.pop("_meta")                        # for KV pricing, not for the table

        # What context this run will actually reserve: never wider than training,
        # and priced, not guessed -- the portal's own estimator, so the bench and
        # the portal's "fits here" verdict cannot disagree about the same file.
        trained = rec.get("trained_ctx") or 0
        ctx = args.ctx if not trained else min(args.ctx, trained)
        kv = modelindex.kv_bytes_at(facts, ctx)
        rec["bench_ctx"], rec["kv_at_ctx_gib"] = ctx, (round(kv / GIB, 2) if kv else None)
        mm = sorted(p for p in path.parent.rglob("*.gguf") if "mmproj" in p.name.lower())
        mm_bytes = (int(row["mmproj_bytes"]) if row and row.get("mmproj_bytes")
                    else sum(p.stat().st_size for p in mm))
        rec["mmproj_gib"] = round(mm_bytes / GIB, 2)

        # A draft/MTP file is not something to benchmark on its own. An earlier run
        # hung on exactly that and had to be killed. There are two shapes on this
        # box and they take different arguments, so decide which one from the file
        # rather than from the repo name:
        #   Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF : full model + blk.32.nextn.*  -> 1 file
        #   unsloth/Qwen3.8-27B-GGUF + MTP/mtp-Qwen3.8-27B-Q4_0.gguf            -> -md
        if _draft_like(spec) or _draft_like(path.name):
            if args.spec == "off":
                print("    SKIPPED: draft/MTP file -- bench it as a draft with --spec mtp "
                      "(it is not a standalone model), or bench the base model beside it")
                rec["skipped"] = "draft-file"
                results.append(rec)
                continue

        draft = None
        head = nextn_tensors(path) if args.spec == "mtp" else None
        if args.spec == "mtp":
            if args.spec_draft:
                draft, why = resolve_draft(args.spec_draft)
                if draft is None:
                    # An explicit --spec-draft that resolves to nothing must not
                    # silently become "use the target's own head": that swaps the
                    # thing being measured and the report still reads as a pass.
                    print(f"    SKIPPED: --spec-draft {args.spec_draft} is {why}. Naming "
                          f"a draft means measuring that draft, so refusing instead of "
                          f"silently falling back to the target's own head.")
                    rec["skipped"] = "draft-not-found"
                    results.append(rec)
                    continue
            elif head:
                # The target carries its own head, so no second file: a sidecar
                # beside it (unsloth ships MTP/mtp-*.gguf next to the 27B that
                # already contains blk.64.nextn.*) would spend 1.3 GiB to do the
                # same job. Point at it explicitly if you want it measured.
                draft = None
                side = find_draft(path)
                if side:
                    print(f"    note: {side} sits beside the target, but the target "
                          f"already carries its own head. Pass --spec-draft {side} "
                          f"to measure the extra file instead (it is an aux file, "
                          f"so it has no cached model id to name).")
            else:
                draft = find_draft(path)
            rec["target_nextn_tensors"] = head
            if draft is None and not head:
                print("    SKIPPED: --spec mtp, but the target has no nextn head and no "
                      "draft file was found beside it. Point at the draft explicitly: "
                      "--spec-draft <repo:QUANT or path.gguf>")
                rec["skipped"] = "no-draft"
                results.append(rec)
                continue
            rec["draft_model"] = str(draft) if draft else "target's own nextn head"
            rec["draft_gib"] = round(draft.stat().st_size / GIB, 2) if draft else 0.0
            print(f"    draft: {rec['draft_model']} ({rec['draft_gib']} GiB); "
                  f"target nextn tensors: {head}")

        # ---- the guard: aperture first, because on this box it is the wall ----
        used, total = aperture()
        # Measured on this box (deepseek-coder-6.7B, 4.46 GiB, ctx 16384, 4 slots):
        # aperture rose 12.8 GiB = weights + ONE KV pool at ctx, so --kv-unified
        # really is one pool and the single-slot price is right. But MemAvailable
        # fell 17.87 -> 1.28 GiB: the file page-in plus llama-server's own
        # --cache-ram (8192 MiB default, per worker). So the aperture needs no
        # slot multiplier while RAM needs the cache priced in, or a bench that
        # "fits" is the one that OOM-kills the router (6.12.8).
        slots = max(1, args.parallel)
        kv_all = kv or int(0.75 * GIB)
        need = (size + mm_bytes + kv_all + SCRATCH_BYTES
                + (draft.stat().st_size if draft else 0))
        need_ram = need + CACHE_RAM_BYTES
        free_ap = total - used
        mem = mem_available()
        moe = (f"MoE {rec['experts']} experts / {rec['experts_used']} active"
               if rec.get("is_moe") else "dense")
        print(f"    file {size/GIB:6.2f} GiB ({rec['shards']})  {moe}  "
              f"trained_ctx {rec.get('trained_ctx')}  "
              f"sw {rec.get('sliding_window')}  FIM tokens "
              f"{'yes' if rec['fim_tokens'] else 'NO'}")
        print(f"    bench ctx {ctx} -> KV {kv_all/GIB:.2f} GiB "
              f"({slots} slots, unified pool), priced from the header")
        print(f"    aperture {used/GIB:.2f}/{total/GIB:.2f} GiB used, free {free_ap/GIB:.2f}; "
              f"needs ~{need/GIB:.2f};  MemAvailable {mem/GIB:.2f} GiB, "
              f"needs ~{need_ram/GIB:.2f} (+{CACHE_RAM_BYTES/GIB:.0f} runtime cache)")
        rec.update({"gtt_used_gib": round(used / GIB, 2), "gtt_total_gib": round(total / GIB, 2),
                    "gtt_free_gib": round(free_ap / GIB, 2),
                    "mem_available_gib": round(mem / GIB, 2), "need_gib": round(need / GIB, 2),
                    "need_ram_gib": round(need_ram / GIB, 2), "slots": slots})
        if args.dry_run:
            print("    dry run: not loading")
            results.append(rec)
            continue
        if need_ram > mem:
            print(f"    SKIPPED: MemAvailable {mem/GIB:.1f} GiB < {need_ram/GIB:.1f} needed "
                  f"(weights + KV + the runtime's own cache). "
                  f"Unload first, or pass --parallel 1 / --ctx.")
            rec["skipped"] = "ram"
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

        # The alias is not cosmetics: a llama-server that can see a models dir and
        # the hub cache holds several models, and a request without a "model" field
        # is answered 400 "model name is missing from the request". Naming it is what
        # makes every probe in this script reach the model it loaded.
        alias = spec
        extra = ["--ctx-size", str(ctx), "--parallel", str(args.parallel), "--kv-unified",
                 "--n-gpu-layers", "99", "--flash-attn", "on", "--alias", alias]
        if args.role == "embed":
            # Measured: a plain server answers /embeddings with 501 "This server
            # does not support embeddings. Start with --embeddings", and /rerank
            # the same until --rerank. A RAG slot is a *flagged* worker, and that
            # is a preset key the portal cannot currently name.
            extra += ["--embeddings"]
            if "rerank" in spec.lower() or "rerank" in path.name.lower():
                extra += ["--rerank"]
        if args.spec == "mtp":
            extra += ["--spec-type", "draft-mtp"]
            if draft:
                extra += ["--spec-draft-model", str(draft)]
        if mm:
            extra += ["--mmproj", str(mm[0])]
        try:
            with Server(path, port[0], extra, log_dir / f"{repo.split('/')[-1]}.log",
                        model_id=alias) as srv:
                # b11146 /props has no top-level n_ctx at all: the slot context is
                # default_generation_settings.n_ctx, and the model card (n_ctx_train,
                # n_params, ftype) lives under /models -> data[].meta (server-context.cpp:4599).
                props = json.loads(srv.get("/props", timeout=20))
                card = next((m for m in (json.loads(srv.get("/models", timeout=20))
                                         .get("data") or []) if m.get("object") == "model"), {})
                cm = card.get("meta") or {}
                slot_ctx = ((props.get("default_generation_settings") or {}).get("n_ctx")
                            or cm.get("n_ctx"))
                rec["served"] = {"slot_n_ctx": slot_ctx,
                                 "total_slots": props.get("total_slots"),
                                 "n_ctx_train": cm.get("n_ctx_train"),
                                 "n_params": cm.get("n_params"),
                                 "served_size_gib": round((cm.get("size") or 0) / GIB, 2),
                                 "ftype": props.get("model_ftype"),
                                 "vision": (props.get("modalities") or {}).get("vision"),
                                 "chat_template_caps": props.get("chat_template_caps")}
                print(f"    served slot_ctx={slot_ctx} slots={props.get('total_slots')} "
                      f"train={cm.get('n_ctx_train')} ftype={props.get('model_ftype')} "
                      f"vision={rec['served']['vision']}")
                if args.role == "embed":
                    bench_embed(srv, rec)
                else:
                    bench_speed(srv, rec, slot_ctx)
                    if args.role in ("fim", "chat", "coder", "agent"):
                        if rec.get("fim_tokens"):
                            bench_fim(srv, rec)
                        else:
                            rec["infill"] = {"skipped": "file carries no FIM tokens"}
                            print("      /infill skipped: no FIM tokens in this vocabulary")
                    if args.role in ("coder", "agent"):
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
