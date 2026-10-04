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
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GIB = 1 << 30
BIN_DEFAULT = "/home/cortana/ai/releases/llama.cpp-b11146/llama-server"
PORT = 8145
REJECT = re.compile(r"option '([^']+)' not recognized in preset '([^']*)'")
# Real names, read out of this build (tools/server/server-task.cpp:1554+). The first version of
# this probe parsed llama:spec_drafts_total, which no build ever published, so every counter came
# back None and the table read as "the draft did nothing" when the truth was "the probe cannot see".
# Names as this build actually emits them (tools/server/server-task.cpp:1554+, prefixed
# "llamacpp:" by the Prometheus writer). History of this probe: it first parsed
# llama:spec_drafts_total (never existed), then the unprefixed names (the writer adds a prefix).
SPEC_METRICS = {
    "drafts": "llamacpp:spec_decode_num_drafts_total",
    "draft_tok": "llamacpp:spec_decode_num_draft_tokens_total",
    "accepted_tok": "llamacpp:spec_decode_num_accepted_tokens_total",
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
    """POST and return the parsed body.

    A non-2xx raises with the SERVER'S OWN reason text attached. urllib's default
    ("HTTP Error 400: Bad Request") hid the one fact that mattered: the router answers
    400 with a JSON body naming the reason, and two probe runs died on the bare status
    line. Never let a probe failure print without that body.
    """
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        try:
            detail = json.dumps(json.loads(detail), ensure_ascii=False)[:600]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} on /{url.rsplit('/', 1)[-1]}: {detail}") from None
    return json.loads(body) if body.lstrip().startswith(("{", "[")) else body


class Router:
    def __init__(self, binary: str, models_dir: str, ini: Path, port: int = PORT,
                 offline: bool = True, hub_cache: str | None = None):
        self.binary, self.port, self.ini, self.models_dir = binary, port, ini, models_dir
        self.hub_cache = hub_cache
        self.dups: list[str] = []
        self.offline = offline
        self.log = Path(f"/tmp/spec_preset_{port}.log")
        self.p = None

    def start(self, wait_s: int = 90) -> str:
        # --no-models-autoload is not optional: --models-dir is the blast radius, and autoload on
        # the real library would start every worker in it beside this probe.
        # --offline is not cosmetic: a model id the router cannot resolve to a cached file does
        # not fail, it DOWNLOADS. Measured on this box: loading ukisai/Swift-1.5-...:MTP started
        # a 12 GiB pull of IQ2_S-mtp.gguf, a different file than the one already on disk. See
        # docs/PORTAL_PLAN.md 6.12.16.
        cmd = [self.binary, "--host", "127.0.0.1", "--port", str(self.port),
               "--models-dir", self.models_dir, "--models-preset", str(self.ini),
               "--no-models-autoload", "--metrics", "-v"]  # /metrics 404s without --metrics
        if self.offline:
            cmd.append("--offline")
        env = dict(os.environ)
        if self.hub_cache:
            # The hub cache is a second search path, and the only way to test id collisions
            # without writing a single byte into the operator's library.
            env["HF_HUB_CACHE"] = self.hub_cache
        self.log.write_text("")
        self.p = subprocess.Popen(cmd, stdout=open(self.log, "wb"),
                                  stderr=subprocess.STDOUT, start_new_session=True, env=env)
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
        """{id: {"state": ..., "args": [...]}} exactly as the runtime reports it.

        Ids are NOT unique. A cached id is the last dash-token of the file name, so a repo
        holding both `X-IQ2_S-mtp.gguf` and `X-IQ3_S-mtp.gguf` lists two models named
        `repo:MTP` — and a preset section applies to one of them. Keeping the last one and
        printing "key reached the worker command line: False" would blame the preset for a
        collision, so the collision is recorded and said out loud.
        """
        out, seen = {}, {}
        self.dups = []
        try:
            for m in get(f"http://127.0.0.1:{self.port}/models").get("data", []):
                st = m.get("status") or {}
                mid, entry = m.get("id"), {"state": st.get("value"), "args": st.get("args") or []}
                seen.setdefault(mid, []).append(entry)
                if mid in out:
                    self.dups.append(mid)
                    if entry["args"] and not out[mid]["args"]:
                        out[mid] = entry      # keep the one that shows a resolved command line
                else:
                    out[mid] = entry
        except Exception as e:
            print(f"    /models failed: {e}")
        return out

    def load(self, model: str, timeout: int = 300) -> bool:
        """Load, and say WHY when it does not. A bare False here once read as 'the preset worked,
        nothing else to report' while the worker was quietly pulling 12 GiB from the hub."""
        try:
            post(f"http://127.0.0.1:{self.port}/models/load", {"model": model}, timeout=timeout)
        except Exception as e:
            print(f"    load failed: {e}")
            return False
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = (self.models().get(model) or {}).get("state")
            if last == "loaded":
                return True
            time.sleep(2)
        print(f"    still not loaded after {timeout}s (last state: {last})")
        hints = [l.strip()[:180] for l in self.log.read_text(errors="replace").splitlines()
                 if model.split("/")[-1] in l and re.search(r"download|error| no |fail", l, re.I)]
        for h in (hints or [l.strip()[:180] for l in self.log.read_text(errors="replace")
                             .splitlines() if " E " in l])[-6:]:
            print(f"      log: {h}")
        return False

    def unload(self, model: str):
        try:
            post(f"http://127.0.0.1:{self.port}/models/unload", {"model": model}, timeout=60)
        except Exception:
            pass

    def spec_counters(self, model: str | None = None) -> dict:
        """Draft counters from /metrics.

        Needs --metrics on the command line: without it the worker answers "This server does not
        support metrics endpoint", which used to arrive as a set of Nones and read as "the draft
        generated nothing". When nothing parses, that is now said out loud, and log_acceptance()
        below is the independent channel.
        """
        # A router answers /metrics by proxying it to a worker, so once more than one model is in
        # play the request has to name the one it means; the bare path stays as a fallback.
        urls = ([f"http://127.0.0.1:{self.port}/metrics?model={model}"] if model else []) + \
               [f"http://127.0.0.1:{self.port}/metrics"]
        text, errs = None, []
        for url in urls:
            try:
                text = get(url, timeout=20)
                break
            except Exception as e:
                errs.append(f"{url.partition('/metrics')[2] or '/metrics'}: {e}")
        if text is None:
            return {"error": "; ".join(errs)}
        if not isinstance(text, str):
            text = json.dumps(text)
        vals = {}
        for label, name in SPEC_METRICS.items():
            m = re.search(rf"^{name}(?:\{{[^}}]*\}})?\s+([0-9.e+]+)", text, re.M)
            vals[label] = float(m.group(1)) if m else None
        if all(v is None for v in vals.values()):
            vals["error"] = ("no spec_decode_* metric in the response — --metrics off, or this "
                             "build names them differently; see the worker's own log line")
        return vals

    def log_acceptance(self) -> dict:
        """The worker's own accounting, straight from its log (printed without --metrics)."""
        text = self.log.read_text(errors="replace")
        out = {}
        m = re.findall(r"draft acceptance = ([0-9.]+) \(\s*(\d+) accepted /\s*(\d+) generated\)",
                       text)
        if m:
            rate, acc, gen = m[-1]
            out = {"log_accept_rate": float(rate), "log_accepted": int(acc), "log_generated": int(gen)}
        s = re.search(r"statistics\s+\S+: #calls\(b,g,a\) = .*?#gen drafts =\s*(\d+), "
                      r"#acc drafts =\s*(\d+), #gen tokens =\s*(\d+), #acc tokens =\s*(\d+)", text)
        if s:
            out["log_stats"] = {"gen_drafts": int(s.group(1)), "acc_drafts": int(s.group(2)),
                                "gen_tokens": int(s.group(3)), "acc_tokens": int(s.group(4))}
        return out

    def stop(self):
        # local copy, and cleared here: every `self.p.` below would otherwise
        # re-read an attribute the type checker cannot narrow across the guard
        proc, self.p = self.p, None
        if proc:
            # A router's model workers are NOT its children — /models/load spawns them loose,
            # so `pkill -P` never sees them. Measured: two probe workers (one holding the Swift
            # file) outlived their router by hours, and the aperture stayed spent. start_new_session
            # made the router a group leader, so the whole group goes together.
            try:
                subprocess.run(["pkill", "-TERM", "-P", str(proc.pid)], timeout=10)
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        time.sleep(3)


def ini_text(models: list[str], sections: list[str] | None, scope: str,
             key: str, value: str, ctx: int) -> str:
    """The presets.ini under test.

    The section defaults to the model id, and that is the whole point: an earlier version wrote
    the key into a section named "[*]" while asking for the model under another name, which
    tested a file the router had no reason to read. `--section` exists to test the *other*
    spelling deliberately — a section named by a directory alias instead of the cache id — which
    is a question worth asking, not an accident.
    """
    sections = sections or models
    head = "[*]\nstop-timeout = 15\n"
    if scope == "global":
        head += f"{key} = {value}\n"
    per = []
    for i, mid in enumerate(models):
        if scope == "model":
            sec = sections[i] if i < len(sections) else mid
            per.append(f"[{sec}]\n{key} = {value}\nctx-size = {ctx}\n")
    return head + "\n" + "\n".join(per)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--binary", default=BIN_DEFAULT)
    ap.add_argument("--model", action="append", default=[],
                    help="model id to inspect (repeatable)")
    ap.add_argument("--key", default="spec-type")
    ap.add_argument("--value", default="draft-mtp")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--scope", choices=["global", "model"], default="model",
                    help="put the key in [*] or in each model's own section")
    ap.add_argument("--control", action="store_true", help="run the mmap control case too")
    ap.add_argument("--models-dir", default="/home/cortana/ai/models/llama",
                    help="library dir for the probe router")
    ap.add_argument("--hub-cache", default=None,
                    help="HF_HUB_CACHE for the probe router only — point it at a throwaway "
                         "hub to test id collisions without touching the real library")
    ap.add_argument("--section", action="append", default=[],
                    help="preset section name(s), in --model order; defaults to the model id. "
                         "A cache id is a NAME token, and the load path may key its preset on "
                         "the file's quantization instead — this flag tests both spellings "
                         "without touching the file on disk")
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

    ini = Path(f"/tmp/spec_preset_{PORT}.ini")
    ini.write_text(ini_text(args.model, args.section, args.scope, args.key, args.value, args.ctx))
    print(f"\npreset under test:\n----\n{ini.read_text()}----")

    r = Router(args.binary, args.models_dir, ini, hub_cache=args.hub_cache)
    print(f"router start: {r.start()}")
    if not r.running:
        r.stop()
        return 1
    try:
        listing = r.models()
        print(f"{len(listing)} models visible to the probe router "
              f"(aperture {aperture_used()/GIB:.2f} GiB — nothing loaded by this probe)")
        if r.dups:
            print(f"    DUPLICATE IDS in the runtime's own listing: {sorted(set(r.dups))}\n"
                  f"    a name collision, not a preset failure — which file answers the id "
                  f"is decided by load-time matching, not by this listing\n")
        else:
            print()
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
                   "duplicate_ids": sorted(set(r.dups)),
                   "key_in_args": has, "args_excerpt": shown, "scope": args.scope,
                   "full_args": args_list}
            if args.measure:
                avail = mem_available()
                print(f"    MemAvailable {avail/GIB:.2f} GiB (floor {args.min_mem_gib:.0f})")
                if avail < args.min_mem_gib * GIB:
                    print("    REFUSED: below the memory floor")
                    rec["skipped"] = "mem floor"
                elif r.load(mid):
                    c0, c1 = r.spec_counters(mid), None
                    # "model" is required, not optional: a router that can see a models dir and
                    # the hub cache holds many models, and an unnamed request is refused with 400.
                    try:
                        out = post(f"http://127.0.0.1:{r.port}/completion",
                                   {"model": mid, "prompt": "def normalize_path(p):\n    return",
                                    "n_predict": 64, "temperature": 0.0}, timeout=600)
                    except RuntimeError as e:
                        print(f"    completion refused: {e}")
                        rec["completion_error"] = str(e)
                        out = {}
                    c1 = r.spec_counters(mid)
                    # A worker that has never generated counts from zero, so a failed or empty
                    # "before" reading is not a reason to report nothing — but it is a reason to
                    # say the run is a single reading, not a difference.
                    base = {k: (c0[k] if c0.get(k) is not None else 0.0) for k in SPEC_METRICS}
                    delta = {k: (None if c1.get(k) is None else c1[k] - base[k])
                             for k in SPEC_METRICS}
                    for label in ("error",):
                        if c1.get(label) or c0.get(label):
                            delta[label] = c1.get(label) or c0.get(label)
                    dt, at = delta.get("draft_tok"), delta.get("accepted_tok")
                    accept = round(at / dt, 3) if dt and at is not None else None
                    tg = (out.get("timings") or {}).get("predicted_per_second")
                    log = r.log_acceptance()
                    print(f"    completion {len(out.get('content') or '')} chars at {tg} tok/s; "
                          f"spec delta {json.dumps(delta)} accept {accept}")
                    if log:
                        print(f"    worker's own log: {json.dumps(log)}")
                    # A loaded worker is the only place the real argv is readable; the unloaded
                    # listing is the router's plan, which is not always what the worker got.
                    live = (r.models().get(mid) or {}).get("args") or []
                    live_spec = [a for a in live if "spec" in a]
                    print(f"    worker argv while loaded: {live_spec or '(no spec args)'}")
                    rec.update({"measured": True, "spec_delta": delta, "accept_rate": accept,
                                "decode_tok_s": tg, "loaded_args_excerpt": live_spec,
                                "log_accounting": log})
                    r.unload(mid)
                else:
                    rec["loaded"] = False
            rows.append(rec)
    finally:
        # Always: kill the router AND its loose workers, and always land the report.
        # A traceback used to leave both a worker holding aperture and no record of why.
        r.stop()
        Path("/tmp/spec_preset_report.json").write_text(json.dumps(rows, indent=1))
    print(f"\nreport: /tmp/spec_preset_report.json")
    print(f"final aperture: {aperture_used()/GIB:.2f} GiB "
          f"(MemAvailable {mem_available()/GIB:.2f} GiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
