#!/usr/bin/env python3
"""Can speculative decoding be turned on per-model by presets.ini, or only by the command line?

This decides whether `--spec-type` can be a personality setting at all. The portal's only write
channel is presets.ini, and a key the runtime refuses there is not ignored — the whole file is
rejected ("option 'mmap' not recognized in preset '*'", the failure that took the managed router
down), so eligibility is per-key, per-build, and must be measured against the binary on this box.

The proof is cheaper than it looks. `GET /models` reports, for every model, `status.args`: the
command line the router will use for that model, printed while the model is still unloaded. So the
preset's effect on a worker is readable without loading anything — no aperture, no risk beside a
router already holding 87 GiB. `--measure` adds the load-and-count case for when the counters
themselves are the question.

Three facts stay separate on purpose:

  1. KEY ELIGIBILITY — does the build parse `spec-type` in `[*]` / in a model section? `mmap` is
     the positive control: it is known to be rejected, which proves the probe can see rejection at
     all. The first time this ran, `--models-dir` pointed at a deleted directory, the router died
     for an unrelated reason, and every key reported "accepted". A green table is not a verdict.
  2. DID IT REACH THE WORKER — `status.args` (or the worker's real argv once loaded).
  3. DID THE DRAFT RUN — /metrics counters after one completion. A model with no nextn head accepts
     the flag and drafts nothing, so a decode number is never read without its draft counters.

Field names are read from the binary, not remembered: b11146 lists models by `id` (not `name`),
reports state in `status.value` (not `status.state`), and takes `{"model": id}` on /models/load.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GIB = 1 << 30
BIN_DEFAULT = "/home/cortana/ai/releases/llama.cpp-b11146/llama-server"
PORT = 8145
REJECT = re.compile(r"option '([^']+)' not recognized in preset '([^']*)'")
SPEC_METRICS = {
    "drafts": "llama:spec_drafts_total",
    "draft_tok": "llama:spec_draft_tokens_total",
    "accepted_tok": "llama:spec_accepted_tokens_total",
}


def aperture_used() -> int:
    for p in ("/sys/class/drm/card1/device/mem_info_gtt_used",
              "/sys/class/drm/card0/device/mem_info_gtt_used"):
        try:
            return int(Path(p).read_text().strip())
        except (OSError, ValueError):
            continue
    return 0


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def get(url: str, timeout: int = 15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.lstrip().startswith(("{", "[")) else body


def post(url: str, payload: dict, timeout: int = 600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.lstrip().startswith(("{", "[")) else body


class Router:
    def __init__(self, binary: str, models_dir: str, ini: Path, port: int = PORT):
        self.binary, self.port, self.ini, self.models_dir = binary, port, ini, models_dir
        self.log = Path(f"/tmp/spec_preset_{port}.log")
        self.p = None

    def start(self, wait_s: int = 90) -> str:
        # --no-models-autoload is not optional: --models-dir is the blast radius, and autoload on
        # the real library would start every worker in it beside this probe.
        cmd = [self.binary, "--host", "127.0.0.1", "--port", str(self.port),
               "--models-dir", self.models_dir, "--models-preset", str(self.ini),
               "--no-models-autoload", "-v"]
        self.log.write_text("")
        self.p = subprocess.Popen(cmd, stdout=open(self.log, "wb"),
                                  stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.p.poll() is not None:
                break
            try:
                get(f"http://127.0.0.1:{self.port}/health", timeout=3)
                break
            except Exception:
                time.sleep(1)
        text = self.log.read_text(errors="replace")
        m = REJECT.search(text)
        if m:
            return f"REJECTED — {m.group(0)}"
        if not self.running:
            first = [l.strip() for l in text.splitlines() if " E " in l]
            return f"exited without loading: {first[:1]}"
        return "healthy"

    @property
    def running(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def models(self) -> dict:
        """{id: {"state": ..., "args": [...]}} exactly as the runtime reports it."""
        out = {}
        try:
            for m in get(f"http://127.0.0.1:{self.port}/models").get("data", []):
                st = m.get("status") or {}
                out[m.get("id")] = {"state": st.get("value"), "args": st.get("args") or []}
        except Exception as e:
            print(f"    /models failed: {e}")
        return out

    def load(self, model: str, timeout: int = 300) -> bool:
        try:
            post(f"http://127.0.0.1:{self.port}/models/load", {"model": model}, timeout=timeout)
        except Exception as e:
            print(f"    load failed: {e}")
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (self.models().get(model) or {}).get("state") == "loaded":
                return True
            time.sleep(2)
        return False

    def unload(self, model: str):
        try:
            post(f"http://127.0.0.1:{self.port}/models/unload", {"model": model}, timeout=60)
        except Exception:
            pass

    def spec_counters(self) -> dict:
        try:
            text = get(f"http://127.0.0.1:{self.port}/metrics", timeout=20)
        except Exception as e:
            return {"error": str(e)}
        vals = {}
        for label, pat in SPEC_METRICS.items():
            m = re.search(rf"^{pat}\{{[^]]*\}}\s+([0-9.e+]+)", text, re.M) or \
                re.search(rf"^{pat}\s+([0-9.e+]+)", text, re.M)
            vals[label] = float(m.group(1)) if m else None
        return vals

    def stop(self):
        if self.running:
            try:
                subprocess.run(["pkill", "-TERM", "-P", str(self.p.pid)], timeout=10)
            except Exception:
                pass
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--binary", default=BIN_DEFAULT)
    ap.add_argument("--model", action="append", default=[],
                    help="model id to inspect (repeatable)")
    ap.add_argument("--key", default="spec-type")
    ap.add_argument("--value", default="draft-mtp")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--scope", choices=["global", "model"], default="model",
                    help="put the key in [*] or in each model's own section")
    ap.add_argument("--control", action="store_true", help="run the mmap control case too")
    ap.add_argument("--measure", action="store_true",
                    help="also load the model and read the spec counters (costs aperture)")
    ap.add_argument("--min-mem-gib", type=float, default=16.0,
                    help="with --measure, refuse to load below this MemAvailable")
    args = ap.parse_args()

    if not Path("/tmp/probe_models").is_dir():
        print("PROBE FIXTURE MISSING: /tmp/probe_models must exist. Without it the runtime exits\n"
              "  ('does not exist or is not a directory') and every verdict below is meaningless.")
        return 2

    rows = []
    if args.control:
        cini = Path("/tmp/spec_preset_control.ini")
        cini.write_text("[*]\nmmap = on\n")
        c = Router(args.binary, "/tmp/probe_models", cini, PORT + 1)
        print(f"control (mmap in [*], known to be rejected): {c.start(wait_s=40)}")
        c.stop()

    sections = []
    per = []
    for mid in args.model:
        if args.scope == "model":
            per.append(f"[{mid}]\n{args.key} = {args.value}\nctx-size = {args.ctx}\n")
    head = "[*]\nstop-timeout = 15\n"
    if args.scope == "global":
        head += f"{args.key} = {args.value}\n"
    ini = Path(f"/tmp/spec_preset_{PORT}.ini")
    ini.write_text(head + "\n" + "\n".join(per))
    print(f"\npreset under test:\n----\n{ini.read_text()}----")

    r = Router(args.binary, "/home/cortana/ai/models/llama", ini)
    print(f"router start: {r.start()}")
    if not r.running:
        r.stop()
        return 1
    listing = r.models()
    print(f"{len(listing)} models visible to the probe router "
          f"(aperture {aperture_used()/GIB:.2f} GiB — nothing loaded by this probe)\n")
    for mid in args.model:
        entry = listing.get(mid)
        print(f"=== {mid}")
        if not entry:
            print("    NOT VISIBLE to this router — the id is wrong or its source is fenced off")
            rows.append({"model": mid, "visible": False})
            continue
        args_list = entry["args"]
        has = any(args.key in a for a in args_list)
        shown = [a for a in args_list if args.key in a or (a == "--spec-draft-model")]
        print(f"    state={entry['state']}  key reached the worker command line: {has}")
        print(f"    relevant args: {shown or '(none)'}")
        rec = {"model": mid, "visible": True, "state": entry["state"],
               "key_in_args": has, "args_excerpt": shown, "scope": args.scope,
               "full_args": args_list}
        if args.measure:
            avail = mem_available()
            print(f"    MemAvailable {avail/GIB:.2f} GiB (floor {args.min_mem_gib:.0f})")
            if avail < args.min_mem_gib * GIB:
                print("    REFUSED: below the memory floor")
                rec["skipped"] = "mem floor"
            elif r.load(mid):
                c0, c1 = r.spec_counters(), None
                out = post(f"http://127.0.0.1:{r.port}/completion",
                           {"prompt": "def normalize_path(p):\n    return", "n_predict": 64,
                            "temperature": 0.0}, timeout=600)
                c1 = r.spec_counters()
                delta = {k: (None if c0.get(k) is None or c1.get(k) is None
                             else c1[k] - c0[k]) for k in SPEC_METRICS}
                accept = (None if not delta.get("draft_tok")
                          else round(delta["accepted_tok"] / delta["draft_tok"], 3))
                tg = (out.get("timings") or {}).get("predicted_per_second")
                print(f"    completion {out.get('tokens')} tok at {tg} tok/s; "
                      f"spec delta {json.dumps(delta)} accept {accept}")
                rec.update({"measured": True, "spec_delta": delta, "accept_rate": accept,
                            "decode_tok_s": tg})
                r.unload(mid)
            else:
                rec["loaded"] = False
        rows.append(rec)
    r.stop()
    Path("/tmp/spec_preset_report.json").write_text(json.dumps(rows, indent=1))
    print(f"\nreport: /tmp/spec_preset_report.json")
    print(f"final aperture: {aperture_used()/GIB:.2f} GiB "
          f"(MemAvailable {mem_available()/GIB:.2f} GiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
