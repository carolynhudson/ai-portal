#!/usr/bin/env python3
"""ttl_oom_lab.py — measure what llama.cpp's router really does with memory.

WHY THIS EXISTS SEPARATELY
    The questions in docs/PORTAL_PLAN.md §6.12 (idle TTL, eviction, `fit`
    accounting, unload-under-load) can only be answered by loading models that
    together exceed the box, which means running them while the production router
    is stopped. This machine *is* the model host: whatever is driving this script
    is being served by that router and dies with it. So this script runs alone,
    detached, and reports to disk.

SURVIVAL RULES (the reason the script is structured the way it is)
    1.  The script must never be the OOM victim. Every llama-server child is
        started in its **own session** (`start_new_session`) with
        `oom_score_adj` raised to +900, so when the kernel needs someone to kill,
        it is the model process and not the harness, the desktop, or this script.
        The parent tries to lower its own score too; that needs privilege, so it
        is best-effort and reported, never assumed.
    2.  The parent stays small. Nothing large is ever read into memory: child
        output goes to files, memory is sampled from /proc, and results are
        appended and flushed as they happen. A run that dies mid-experiment still
        leaves everything up to the last sample.
    3.  The router always comes back. Cleanup runs on success, on error, on
        Ctrl-C, on SIGTERM, and on exit. It also runs *before* starting, if a
        previous run left a `state.json` behind — a killed harness must not leave
        the box without a router.
    4.  A memory guardian samples every second and, unless --allow-oom is passed,
        aborts the experiment and kills the children when MemAvailable drops
        below --floor-gb. That is deliberate: what we want to learn is what
        llama.cpp does when it cannot allocate, not whether this desktop survives
        a kernel OOM. It still lets an attempt proceed until memory actually runs
        short, which is what makes the answer real.

    No third-party imports: this has to run from /usr/bin/python3 with no venv.
    No network beyond 127.0.0.1. Nothing is deleted. The production presets file
    is never written — experiments get their own copy in a temp dir, and models
    are reached through a symlink farm, so the library itself is read-only.

USAGE
    python3 tools/ttl_oom_lab.py --list-models          # what it can see
    python3 tools/ttl_oom_lab.py --dry-run              # the whole plan, touches nothing
    python3 tools/ttl_oom_lab.py --mini                 # plumbing test, router untouched, ~1 GB
    python3 tools/ttl_oom_lab.py --full --yes           # THE TEST (stops the router, ~110 GB)
    python3 tools/ttl_oom_lab.py --background --full --yes   # ... and survive your own death
    python3 tools/ttl_oom_lab.py --restore-router-only  # emergency: put the router back

    Results: ~/ai/logs/ttl-oom/<UTC timestamp>/   (also symlinked as .../latest)
        summary.md   what happened, in order, with the numbers
        results.json machine-readable record of every phase
        samples.csv  1 Hz: MemAvailable, SwapFree, per-child RSS/anon/file
        events.log   llama.cpp's own load/unload/evict lines, with timestamps
        logs/        full stdout+stderr per server process
"""
from __future__ import annotations

import argparse
import atexit
import csv
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

AI = Path.home() / "ai"
ROUTER_UNIT = "ai-llama-router"
MODELS_DIR = AI / "models" / "llama"
PROD_PRESETS = AI / "config" / "llama" / "presets.ini"
LOGROOT = AI / "logs" / "ttl-oom"
GB = 1 << 30

# Auxiliary .gguf files are not models, and always win a naive size contest.
# Same convention as the portal (app/core/modeladd.py) — one opinion, two homes.
AUX = re.compile(r"^(?:mmproj|mtp|draft|imatrix|adapter)-", re.I)
# These are not chat/completion models — an experiment that picked one would
# measure a server error instead of a memory behaviour. Name heuristic only; a
# real read of the `bert` architecture lives in the portal index (Phase N).
NOT_GENERATIVE = re.compile(r"(emb|embed|rerank|bge|e5-|minilm|bert|clip|vit)", re.I)
SHARD = re.compile(r"^(?P<base>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})\.gguf$", re.I)
# llama.cpp's own words for the things under test. Parsed rather than inferred:
# evidence beats interpretation, and these strings change between releases.
EVENT_RE = re.compile(
    r"(evicting idle LRU|unload_lru|unloading model|loading model|"
    r"model_loaded|is not running|entering sleeping state|"
    r"exiting sleeping state|failed to allocate|unable to allocate)", re.I)


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024.0
    return f"{n:.1f} GB"


def run(cmd, timeout=120):
    """Run a command without a shell. Never raises: an experiment that cannot
    talk to systemd should report that, not die from it."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:                                    # noqa: BLE001
        return 125, f"{type(e).__name__}: {e}"


def meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            out[k] = int(rest.split()[0]) * 1024          # kB -> bytes
    except Exception:                                       # noqa: BLE001
        pass
    return out


# NOTE ON THE unchecked-throwing-call-python LENS RULE:
# It flags every int() and open() below. They are deliberate, not oversights:
#   * int(m.group(1)) after r"(\d+)" cannot raise (the regex is the guard).
#   * The open()s happen after state.json is written, so an exception MUST reach
#     atexit -> cleanup() and put the router back. Wrapping them in try/except
#     would swallow the very error that cleanup needs to see. The one open() that
#     runs *before* any state (--background) is allowed to crash loudly because
#     "the experiment did not start" is the correct outcome there.
# This file's whole contract is: whatever happens, the router comes back. See
# Lab.cleanup() and the state.json recovery in main().


def proc_mem(pid: int) -> dict:
    """rss / anon / file-backed bytes. Anon is the number that matters: file-backed
    pages can be reclaimed under pressure, anonymous ones cannot."""
    try:
        st = Path(f"/proc/{pid}/status").read_text()
    except Exception:                                       # noqa: BLE001
        return {}
    got = {}
    for key, name in (("VmRSS", "rss"), ("RssAnon", "anon"), ("RssFile", "file"),
                      ("RssShmem", "shmem")):
        m = re.search(rf"^{key}:\s+(\d+) kB", st, re.M)
        got[name] = int(m.group(1)) * 1024 if m else 0
    return got


def find_models() -> tuple:
    """Loadable models, plus notes: (models, notes).

    Shards collapsed to their whole size, auxiliary files and incomplete shard sets
    dropped, dangling links dropped.

    Dangling links are reported rather than skipped silently — one of them is what
    crashed the original gguf-indexer, and a model that cannot be found because a
    symlink points at a deleted blob is exactly the kind of thing that makes an
    experiment look like a llama.cpp failure.
    """
    out, dangling, incomplete = [], [], []
    if not MODELS_DIR.is_dir():
        return out, {}
    groups = {}
    for d in sorted(MODELS_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            files = list(d.rglob("*.gguf"))
        except OSError:
            continue
        for f in files:
            if AUX.search(f.name):
                continue
            if f.is_symlink() and not f.exists():
                dangling.append(str(f))
                continue
            if not f.is_file():
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if NOT_GENERATIVE.search(d.name):
                continue
            m = SHARD.match(f.name)
            if m:
                groups.setdefault((str(d), m["base"]), {})[m["i"]] = (f, size, m["n"])
            elif size > 20 * 1024 * 1024:
                out.append({"name": d.name, "path": str(f), "bytes": size, "parts": 1})
    for (parent, base), parts in groups.items():
        try:
            want = int(next(iter(parts.values()))[2])
        except (ValueError, StopIteration):
            continue
        if len(parts) < want or "00001" not in parts:
            incomplete.append(f"{base} ({len(parts)}/{want} parts)")
            continue
        p, _, _ = parts["00001"]
        out.append({"name": Path(parent).name, "path": str(p),
                    "bytes": sum(v[1] for v in parts.values()), "parts": want})
    out.sort(key=lambda x: x["bytes"])
    notes = {}
    if dangling:
        notes["dangling_links"] = dangling
    if incomplete:
        notes["incomplete_shard_sets"] = incomplete
    return out, notes


class Lab:
    """One run: output dir, children, sampler, and the promise about the router."""

    def __init__(self, args):
        self.args = args
        self.started = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        # A dry run must leave nothing behind in the results tree — it is how you
        # ask "what would you do", and littering that answer teaches nobody to ask.
        self.dir = (Path("/tmp") / "ttl-oom-dryrun" / stamp
                    if args.mode == "dry-run" else LOGROOT / stamp)
        self.logs = self.dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        try:
            if self.dir.parent == LOGROOT:
                latest = LOGROOT / "latest"
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(self.dir, target_is_directory=True)
        except OSError:
            pass
        self.results = {"started": ts(), "argv": sys.argv[1:], "mode": args.mode,
                        "phases": [], "notes": []}
        self.children = []                            # (Popen, name, logfile)
        self.events = []
        self.aborted = None
        self._stop = threading.Event()
        self._cleanup_done = False
        self._router_stopped_by_me = False
        self.samples = open(self.dir / "samples.csv", "w", buffering=1)
        self.writer = csv.writer(self.samples)
        self.writer.writerow(["t_s", "phase", "mem_available_gb", "swap_free_gb",
                              "child", "child_rss_gb", "child_anon_gb",
                              "child_file_gb"])
        self.phase = "init"
        self.state = self.dir / "state.json"

    # ------------------------------------------------------------- reporting
    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.dir / "run.log", "a") as f:
            f.write(line + "\n")

    def note(self, key: str, value) -> None:
        self.results["notes"].append({key: value, "at": ts()})
        self.say(f"note {key}: {value}")

    def phase_ctx(self, name: str):
        return _Phase(self, name)

    def settle(self, secs: float = 10.0) -> float:
        """Let the kernel catch up after a stop, and record whether it did. If
        stopping the router did not free the memory, the experiment is measuring
        something else's footprint and should say so out loud."""
        before = meminfo().get("MemAvailable", 0)
        time.sleep(secs)
        after = meminfo().get("MemAvailable", 0)
        self.sample()
        self.say(f"memory settled: {human(before)} -> {human(after)}")
        return after

    def sample(self, why: str = "") -> None:
        m = meminfo()
        row_av = m.get("MemAvailable", 0) / GB
        row_sw = m.get("SwapFree", 0) / GB
        if not self.children:
            self.writer.writerow([round(time.time() - self.started, 1), self.phase,
                                  f"{row_av:.2f}", f"{row_sw:.2f}", "", "", "", ""])
            return
        for proc, name, _ in self.children:
            pm = proc_mem(proc.pid)
            self.writer.writerow([round(time.time() - self.started, 1), self.phase,
                                  f"{row_av:.2f}", f"{row_sw:.2f}", name,
                                  f"{pm.get('rss', 0) / GB:.2f}",
                                  f"{pm.get('anon', 0) / GB:.2f}",
                                  f"{pm.get('file', 0) / GB:.2f}"])

    def _sampler(self) -> None:
        while not self._stop.wait(self.args.sample_secs):
            try:
                self.sample()
                avail = meminfo().get("MemAvailable", 0)
                floor = self.args.floor_gb * GB
                if (avail < floor and not self.args.allow_oom
                        and self.children and not self.aborted):
                    self.aborted = (f"guardian: MemAvailable {avail / GB:.1f} GB < "
                                    f"--floor-gb {self.args.floor_gb}")
                    self.say("ABORTING: " + self.aborted)
                    self.kill_children("guardian")
            except Exception as e:  # a sampler that dies stops the evidence
                self.say(f"sampler error (continuing): {type(e).__name__}: {e}")

    # -------------------------------------------------------------- children
    def spawn(self, cmd, name: str, env=None):
        logpath = self.logs / f"{name}.log"
        fh = open(logpath, "a")
        fh.write(f"\n===== {ts()} spawn: {' '.join(cmd)}\n")
        fh.flush()
        # A new session: the child outlives a terminal hangup, and can be killed
        # by group. Output to a file, never a pipe — a full pipe can wedge a parent.
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True, cwd=str(AI))
        self.children.append((proc, name, logpath))
        # Ask the kernel to prefer this process if memory runs out. Raising our own
        # child's score needs no privilege; the parent lowering its own usually does.
        try:
            Path(f"/proc/{proc.pid}/oom_score_adj").write_text("900")
            self.say(f"spawned {name} pid={proc.pid} oom_score_adj=900 -> {logpath.name}")
        except OSError as e:
            self.say(f"spawned {name} pid={proc.pid} (could not set oom_score_adj: {e})")
        return proc, logpath

    def kill_children(self, why: str) -> None:
        for proc, name, _ in self.children:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                self.say(f"SIGTERM {name} (pid {proc.pid}) [{why}]")
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 30
        for proc, name, _ in self.children:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.5)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    self.say(f"SIGKILL {name} (pid {proc.pid}) [{why}]")
                except (ProcessLookupError, PermissionError):
                    pass

    # ------------------------------------------------------------------ http
    def api(self, port: int, path: str, payload=None, timeout=10, method=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace") if e.fp else str(e)
        except Exception as e:                              # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"

    def wait_up(self, port: int, timeout=180) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, _ = self.api(port, "/health", timeout=3)
            if code in (200, 400, 404, 412, 503):   # anything answering is up
                return True
            time.sleep(2)
        return False

    # ------------------------------------------------------------- router IO
    def events_from(self, logpath: Path) -> None:
        """Append llama.cpp's own load/unload/evict lines since the last read."""
        try:
            with open(logpath) as f:
                f.seek(self._offsets.get(logpath, 0))
                chunk = f.read()
                self._offsets[logpath] = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            if EVENT_RE.search(line):
                self.events.append(line.strip()[:200])
        with open(self.dir / "events.log", "a") as f:
            for line in chunk.splitlines():
                if EVENT_RE.search(line):
                    f.write(line.strip() + "\n")
            f.flush()

    _offsets = {}

    # -------------------------------------------------------- router lifecycle
    def stop_router(self) -> None:
        rc, out = run(["systemctl", "--user", "stop", ROUTER_UNIT], timeout=120)
        self._router_stopped_by_me = True
        # The marker is written *before* the memory is confirmed free: if this
        # process is killed in the next second, whoever starts next has to know
        # the router went down on purpose and put it back.
        self._write_state(stopped_router=True)
        rc2, st = run(["systemctl", "--user", "is-active", ROUTER_UNIT], timeout=30)
        self.say(f"router stopped (rc={rc}, now {st.strip() or '?'})")
        freed = self.settle(15)
        self.results["mem_after_router_stop_gb"] = round(freed / GB, 1)
        # MemAvailable can be ~117 GB with the router stopped and still be a lie:
        # an idle llama.cpp under --fit holds weights as *reclaimable* pages that
        # the kernel is happy to call "available" right up until a real allocation
        # has to happen. The stop not moving the number is normal (an idle router
        # was never the thing pinning the memory); the guardian, not this figure,
        # is what stops us discovering reclaimability the hard way.
        self.say("(if this barely moved: an idle router's pages were reclaimable, "
                 "not resident — MemAvailable counts them either way)")

    def start_router(self) -> None:
        run(["systemctl", "--user", "start", ROUTER_UNIT], timeout=120)
        ok = False
        for _ in range(40):
            rc, st = run(["systemctl", "--user", "is-active", ROUTER_UNIT], timeout=30)
            if (st or "").strip() == "active":
                ok = True
                break
            time.sleep(3)
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("127.0.0.1", 8080))
            port_ok = True
        except OSError:
            port_ok = False
        finally:
            s.close()
        self.results["router_restored"] = {"active": ok, "port_8080": port_ok,
                                           "at": ts()}
        self.say(f"router restored: active={ok} port8080={port_ok}")
        if ok:
            self.state.unlink(missing_ok=True)
            self._router_stopped_by_me = False

    def _write_state(self, **kw) -> None:
        cur = {}
        try:
            cur = json.loads(self.state.read_text())
        except Exception:                                   # noqa: BLE001
            pass
        cur.update(kw)
        cur.update({"pid": os.getpid(), "dir": str(self.dir), "at": ts()})
        self.state.write_text(json.dumps(cur, indent=2))

    # ---------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._stop.set()
        try:
            if self.children:
                self.kill_children("cleanup")
            if self._router_stopped_by_me:
                self.start_router()
        finally:
            self.write_results()
            try:
                self.samples.close()
            except Exception:                               # noqa: BLE001
                pass

    def write_results(self) -> None:
        self.results["finished"] = ts()
        self.results["elapsed_s"] = round(time.time() - self.started, 1)
        self.results["events"] = self.events[-200:]
        (self.dir / "results.json").write_text(json.dumps(self.results, indent=2))
        lines = [f"# ttl_oom_lab run {self.results['started']}", "",
                 f"- mode: `{self.args.mode}`  elapsed: {self.results['elapsed_s']} s",
                 f"- argv: `{getattr(self.args, '_argv', 'n/a')}`",
                 f"- guardian floor: {self.args.floor_gb} GB "
                 f"({'DISABLED' if self.args.allow_oom else 'active'})",
                 f"- aborted: {self.aborted or 'no'}",
                 f"- router restored: {self.results.get('router_restored', 'was not stopped')}",
                 "", "## phases", ""]
        for p in self.results["phases"]:
            lines.append(f"### {p['name']} — {p['status']}"
                         + (f" ({p.get('error')})" if p.get("error") else ""))
            for k, v in (p.get("data") or {}).items():
                lines.append(f"- **{k}**: {v}")
            lines.append("")
        if self.events:
            lines += ["## llama.cpp said", "```"] + self.events[-60:] + ["```", ""]
        (self.dir / "summary.md").write_text("\n".join(lines))


class _Phase:
    def __init__(self, lab, name):
        self.lab, self.name = lab, name

    def __enter__(self):
        self.lab.phase = self.name
        self.rec = {"name": self.name, "status": "running", "started": ts(), "data": {}}
        self.lab.results["phases"].append(self.rec)
        self.lab.say(f"=== {self.name}")
        self.t0 = time.time()
        return self.rec

    def __exit__(self, et, ev, tb):
        self.rec["elapsed_s"] = round(time.time() - self.t0, 1)
        if et is None:
            self.rec["status"] = "ok"
        else:
            self.rec["status"] = "error"
            self.rec["error"] = f"{et.__name__}: {ev}"[:300]
            self.lab.say(f"!!! {self.name} failed: {self.rec['error']}")
        self.lab.phase = "between"
        return False            # never swallow: an error must reach the caller


# ---------------------------------------------------------------- experiment kit
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_presets(path: Path, sections: dict) -> None:
    """A presets file for this experiment only. The production file is never
    touched: a bad key here should fail the experiment, not the box."""
    lines = ["; ttl_oom_lab experiment presets — safe to delete"]
    for name, kv in sections.items():
        lines += ["", f"[{name}]"]
        lines += [f"{k} = {v}" for k, v in kv.items()]
    path.write_text("\n".join(lines) + "\n")


def write_farm(root: Path, names, models) -> None:
    """Symlink farm of the models under test — the router's --models-dir is a
    directory of model dirs, and this is also what a personality will be."""
    root.mkdir(parents=True, exist_ok=True)
    by_name = {m["name"]: m for m in models}
    for n in names:
        m = by_name[n]
        src = Path(m["path"]).parent
        dst = root / n
        if not dst.exists():
            dst.symlink_to(src, target_is_directory=True)


def server_cmd(binpath: Path, farm: Path, presets: Path, port: int, models_max: int,
               extra=None) -> list:
    cmd = [str(binpath), "--host", "127.0.0.1", "--port", str(port),
           "--models-dir", str(farm), "--models-preset", str(presets),
           "--models-max", str(models_max), "--no-webui"]
    return cmd + list(extra or [])


def model_status(lab: Lab, port: int, model: str) -> str:
    """One word: unloaded | loading | loaded | sleeping | error(...) |
    not_registered. `GET /models` reports a *dict* per model
    ({'value': 'loading', 'args': [...]}), so accept dict or bare string — and
    never treat an HTTP 200 from /models/load as 'loaded': that call is an ack
    and the load happens afterwards, asynchronously."""
    code, body = lab.api(port, "/models", timeout=20)
    if code != 200:
        return f"error(http {code})"
    try:
        data = json.loads(body).get("data", [])
    except Exception as e:                                  # noqa: BLE001
        return f"error({type(e).__name__})"
    for d in data:
        if (d.get("id") or d.get("name")) != model:
            continue
        st = d.get("status")
        if isinstance(st, dict):
            return str(st.get("value", "?"))
        return str(st if st is not None else "?")
    return "not_registered"


def wait_status(lab: Lab, port: int, model: str, want=("loaded", "sleeping"),
                timeout=900, logpath=None, poll=5.0):
    """Block until the model is actually resident (or the wait fails). Returns
    (status, seconds). `error` / `unloaded` are terminal too: a load that failed
    must not be waited on for the full timeout."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = model_status(lab, port, model)
        if st != last:
            lab.say(f"    {model}: {st} (+{time.time() - t0:.0f}s)")
            last = st
        if st in tuple(want) + ("error", "not_registered"):
            return st, round(time.time() - t0, 1)
        if logpath:
            lab.events_from(logpath)
        time.sleep(poll)
    return f"timeout(last={last})", round(time.time() - t0, 1)


def load_model(lab: Lab, port: int, model: str, timeout=900, logpath=None):
    """POST /models/load and *wait for it*, measuring the real load. Returns
    (status, seconds). The first version of this recorded the ack's latency as
    the load time — 0.0 s — and then happily asked for three 87 GB models at once,
    because nothing in the response said 'still loading'."""
    t0 = time.time()
    code, body = lab.api(port, "/models/load", {"model": model}, timeout=60)
    if code != 200:
        return f"http {code}: {body[:100]}", round(time.time() - t0, 1)
    return wait_status(lab, port, model, ("loaded", "sleeping"), timeout, logpath)


def unload_model(lab: Lab, port: int, model: str, timeout=300, logpath=None):
    t0 = time.time()
    code, body = lab.api(port, "/models/unload", {"model": model}, timeout=60)
    if code != 200:
        return f"http {code}: {body[:100]}", round(time.time() - t0, 1)
    return wait_status(lab, port, model, ("unloaded",), timeout, logpath)


def complete(lab: Lab, port: int, model: str, tokens=8, timeout=1800):
    """Generate a few tokens. /completion rather than /v1/chat/completions on
    purpose: chat templates are a second variable (three models on this box need
    `jinja = true` to render at all), and an experiment about memory should not
    be confounded by a template error that looks like a load failure."""
    t0 = time.time()
    code, body = lab.api(port, "/completion", {
        "model": model, "prompt": "Once upon a time", "n_predict": tokens,
        "stream": False}, timeout=timeout)
    if code in (404,) or (code == 400 and "not found" in body.lower()):
        # not proxied on this build — fall back to the OpenAI-shaped route so a
        # missing endpoint does not read as "the model failed to load"
        code, body = lab.api(port, "/v1/completions", {
            "model": model, "prompt": "Once upon a time", "max_tokens": tokens},
            timeout=timeout)
    return round(time.time() - t0, 2), code, body[:400]


def models_state(lab: Lab, port: int) -> dict:
    code, body = lab.api(port, "/models", timeout=15)
    try:
        data = json.loads(body).get("data", [])
    except Exception:                                       # noqa: BLE001
        return {"error": body[:200]}
    return {d.get("id") or d.get("name"): d.get("status") for d in data}


# ---------------------------------------------------------------- experiments
def exp_idle_ttl(lab: Lab, models, binpath) -> None:
    """E0 — does an idle model ever get unloaded by itself?"""
    # Not the biggest model: this experiment needs an idle window, and a 94 GB
    # load would spend most of it waiting. Anything big enough to show a memory
    # plateau is big enough to show it being taken back (or not).
    m = next((x for x in models if 8 * GB <= x["bytes"] <= 40 * GB), models[-1])
    port = free_port()
    with lab.phase_ctx("E0 idle model never expires") as rec:
        farm = lab.dir / "farm-e0"
        write_farm(farm, [m["name"]], models)
        presets = lab.dir / "e0.ini"
        write_presets(presets, {"*": {"threads": 16, "fit": "on", "fit-target": 8192,
                                      "stop-timeout": 15}})
        proc, logp = lab.spawn(server_cmd(binpath, farm, presets, port, 1), "e0-server")
        if not lab.wait_up(port, 120):
            rec["data"]["error"] = "server did not come up"
            return
        t, code, _ = complete(lab, port, m["name"])
        rec["data"]["loaded_and_generated"] = f"{m['name']} in {t}s (http {code})"
        rss_after_load = proc_mem(proc.pid).get("rss", 0)
        idle = lab.args.idle_secs
        lab.say(f"idling {idle}s — does anything unload it?")
        t0 = time.time()
        while time.time() - t0 < idle and proc.poll() is None:
            time.sleep(10)
            lab.events_from(logp)
            lab.sample()
        pm = proc_mem(proc.pid) if proc.poll() is None else {}
        rec["data"]["rss_after_load_gb"] = round(rss_after_load / GB, 2)
        rec["data"][f"rss_after_{idle}s_idle_gb"] = round(pm.get("rss", 0) / GB, 2)
        rec["data"]["still_resident"] = bool(pm)
        rec["data"]["verdict"] = (
            "no idle expiry: RSS unchanged with an empty queue"
            if pm.get("rss", 0) > rss_after_load * 0.9 else
            "memory dropped while idle — check events.log for what did it")
        lab.kill_children("E0 done")


def exp_fit_accounting(lab: Lab, models, binpath) -> None:
    """E1 — the expensive question. Does `fit` see the *other* resident model?

    Two models that each individually fit in the free memory, and together do
    not, loaded with `fit = on` and `--models-max 2`. Three possible answers, all
    informative: the second load is refused; it succeeds by shrinking the context;
    or it proceeds and the box runs out — which is the OOM the portal is meant to
    prevent, and the reason this experiment needs the guardian.
    """
    # The two largest. Picking a marginal pair would let `fit` succeed by shaving
    # a little context and make the answer ambiguous; a pair that is 150 GB against
    # 110 GB can only end one of three ways, and all three are worth knowing.
    cands = sorted([m for m in models if m["bytes"] > 8 * GB],
                   key=lambda m: m["bytes"], reverse=True)
    if len(cands) < 2:
        raise RuntimeError("need two large models for E1")
    a, b = cands[0], cands[1]
    port = free_port()
    with lab.phase_ctx("E1 does fit account for other residents") as rec:
        farm = lab.dir / "farm-e1"
        write_farm(farm, [a["name"], b["name"]], models)
        presets = lab.dir / "e1.ini"
        write_presets(presets, {"*": {"threads": 16, "fit": "on",
                                      "fit-target": lab.args.fit_target,   # MiB, as in presets.ini
                                      "stop-timeout": 15}})
        proc, logp = lab.spawn(server_cmd(binpath, farm, presets, port, 2), "e1-server")
        if not lab.wait_up(port, 120):
            rec["data"]["error"] = "server did not come up"
            return
        rec["data"]["free_before_gb"] = round(meminfo().get("MemAvailable", 0) / GB, 1)
        rec["data"]["joint_demand_gb"] = round((a["bytes"] + b["bytes"]) / GB, 1)
        rec["data"]["question"] = ("each fits alone, together they do not: does the "
                                   "second load get refused, shrink, or start a "
                                   "memory emergency?")
        for i, m in enumerate((a, b), 1):
            t0 = time.time()
            code, body = lab.api(port, "/models/load", {"model": m["name"]}, timeout=1800)
            secs = round(time.time() - t0, 1)
            pm = proc_mem(proc.pid)
            rec["data"][f"load{i} {m['name']} ({human(m['bytes'])})"] = (
                f"http {code} in {secs}s · rss {pm.get('rss', 0) / GB:.1f} GB "
                f"(anon {pm.get('anon', 0) / GB:.1f}) · "
                f"free {meminfo().get('MemAvailable', 0) / GB:.1f} GB"
                + (f" · {body[:120]}" if code >= 400 else ""))
            lab.events_from(logp)
            lab.sample()
            if lab.aborted:
                rec["data"]["guardian"] = lab.aborted
                break
        rec["data"]["model_states"] = models_state(lab, port)
        lab.events_from(logp)
        rec["data"]["events_during"] = lab.events[-6:]
        lab.kill_children("E1 done")


def exp_lru_thrash(lab: Lab, models, binpath) -> None:
    """E2 — with models_max=2 and three models in demand, does it thrash?"""
    cands = [m for m in models if m["bytes"] > 3 * GB] or models[-3:]
    trio = cands[:3] if len(cands) >= 3 else cands
    port = free_port()
    with lab.phase_ctx("E2 LRU thrash with models_max=2") as rec:
        farm = lab.dir / "farm-e2"
        write_farm(farm, [m["name"] for m in trio], models)
        presets = lab.dir / "e2.ini"
        write_presets(presets, {"*": {"threads": 16, "fit": "on", "fit-target": 8192,
                                      "stop-timeout": 15}})
        proc, logp = lab.spawn(server_cmd(binpath, farm, presets, port, 2), "e2-server")
        if not lab.wait_up(port, 120):
            rec["data"]["error"] = "server did not come up"
            return
        times = {}
        for rnd in range(lab.args.cycles):
            for m in trio:
                t, code, _ = complete(lab, port, m["name"], tokens=4, timeout=1800)
                times.setdefault(m["name"], []).append(t)
                lab.events_from(logp)
                lab.sample()
                if lab.aborted:
                    break
            if lab.aborted:
                rec["data"]["guardian"] = lab.aborted
                break
        rec["data"]["per_call_seconds"] = {
            k: [round(x, 1) for x in v] for k, v in times.items()}
        # The thrash question, in numbers. `first` is cold (it pays the load);
        # `warm_max` is the slowest repeat of the same model while the others are
        # resident. Warm ~= first means every call pays a load, which is what a
        # model that never expires under a too-small models_max looks like.
        thrash = {}
        for k, v in times.items():
            rest = v[1:] or [0]
            thrash[k] = f"first {v[0]:.1f}s · warm max {max(rest):.1f}s"
        rec["data"]["per_model"] = thrash
        rec["data"]["every_call_pays_a_load"] = bool(times) and all(
            len(v) > 1 and min(v[1:]) >= v[0] * 0.75 for v in times.values())
        rec["data"]["load_unload_events"] = len(lab.events)
        lab.kill_children("E2 done")


def exp_unload_in_flight(lab: Lab, models, binpath) -> None:
    """E3 — can a TTL interrupt a generation? If yes, it is not a TTL."""
    m = next((x for x in models if x["bytes"] > 4 * GB), None) or models[-1]
    port = free_port()
    with lab.phase_ctx("E3 unload while generating") as rec:
        farm = lab.dir / "farm-e3"
        write_farm(farm, [m["name"]], models)
        presets = lab.dir / "e3.ini"
        write_presets(presets, {"*": {"threads": 16, "fit": "on", "fit-target": 8192}})
        proc, logp = lab.spawn(server_cmd(binpath, farm, presets, port, 1), "e3-server")
        if not lab.wait_up(port, 180):
            rec["data"]["error"] = "server did not come up"
            return
        complete(lab, port, m["name"], tokens=4)          # make sure it is resident
        result = {}

        def gen():
            t0 = time.time()
            code, body = lab.api(port, "/v1/chat/completions", {
                "model": m["name"],
                "messages": [{"role": "user", "content": "write 400 words about memory"}],
                "max_tokens": 400, "stream": False}, timeout=1800)
            result.update(code=code, secs=round(time.time() - t0, 1),
                          body=body[:200])
        th = threading.Thread(target=gen, daemon=True)
        th.start()
        time.sleep(4)
        code, body = lab.api(port, "/models/unload", {"model": m["name"]}, timeout=30)
        rec["data"]["unload_http"] = f"{code} {body[:120]}"
        th.join(timeout=600)
        rec["data"]["in_flight_request"] = (f"http {result.get('code')} in "
                                            f"{result.get('secs')}s — "
                                            f"{str(result.get('body'))[:120]}")
        lab.events_from(logp)
        rec["data"]["events"] = lab.events[-4:]
        lab.kill_children("E3 done")


def exp_unload_frees(lab: Lab, models, binpath) -> None:
    """E4 — does unload return the memory, and what does a reload cost?"""
    pick = [m for m in models if m["bytes"] > 4 * GB][: lab.args.models_n]
    if not pick:
        pick = models[-2:]
    port = free_port()
    with lab.phase_ctx("E4 unload frees memory / reload cost") as rec:
        farm = lab.dir / "farm-e4"
        write_farm(farm, [m["name"] for m in pick], models)
        presets = lab.dir / "e4.ini"
        write_presets(presets, {"*": {"threads": 16, "fit": "on", "fit-target": 8192}})
        proc, logp = lab.spawn(server_cmd(binpath, farm, presets, port, 1), "e4-server")
        if not lab.wait_up(port, 180):
            rec["data"]["error"] = "server did not come up"
            return
        base = proc_mem(proc.pid).get("rss", 0)
        rec["data"]["baseline_rss_gb"] = round(base / GB, 2)
        for m in pick:
            t0 = time.time()
            lab.api(port, "/models/load", {"model": m["name"]}, timeout=1800)
            load_s = round(time.time() - t0, 1)
            loaded = proc_mem(proc.pid).get("rss", 0)
            t0 = time.time()
            lab.api(port, "/models/unload", {"model": m["name"]}, timeout=120)
            time.sleep(8)
            after = proc_mem(proc.pid).get("rss", 0)
            t0 = time.time()
            lab.api(port, "/models/load", {"model": m["name"]}, timeout=1800)
            reload_s = round(time.time() - t0, 1)
            rec["data"][m["name"]] = (
                f"{human(m['bytes'])}: load {load_s}s → rss {(loaded - base) / GB:.1f} GB "
                f"above baseline; unload {round(time.time() - t0, 1)}s → "
                f"{(after - base) / GB:.1f} GB still held; reload {reload_s}s")
            lab.events_from(logp)
            lab.sample()
        lab.kill_children("E4 done")


EXPERIMENTS = {
    "E0": ("idle model never expires", exp_idle_ttl),
    "E1": ("does fit see other residents", exp_fit_accounting),
    "E2": ("LRU thrash at models_max=2", exp_lru_thrash),
    "E3": ("unload while generating", exp_unload_in_flight),
    "E4": ("unload frees / reload cost", exp_unload_frees),
}


# ---------------------------------------------------------------------- main
def preflight(args) -> tuple:
    problems, notes = [], []
    def build_of(p: Path) -> int:
        # llama.cpp-b10964 -> 10964. Anything unparseable sorts lowest rather than
        # raising: a stray directory named llama.cpp-something must not crash
        # preflight and strand the caller with no answer at all.
        m = re.search(r"-b(\d+)$", p.parent.name)
        return int(m.group(1)) if m else -1
    bins = sorted((AI / "releases").glob("llama.cpp-b*/llama-server"), key=build_of)
    if not bins:
        problems.append(f"no llama-server under {AI}/releases")
    binpath = Path(args.bin) if args.bin else (bins[-1] if bins else None)
    if binpath and not Path(binpath).is_file():
        problems.append(f"--bin not found: {binpath}")
    models, model_notes = find_models()
    if not models:
        problems.append(f"no loadable models under {MODELS_DIR}")
    # A running llama-server is *expected* — full mode stops the router itself.
    # What matters is whether enough memory is free after that stop, so this is a
    # note; only the memory figure below is allowed to refuse a run.
    others = [l for l in run(["pgrep", "-a", "-f", "llama-server"])[1].splitlines()
              if "ttl_oom_lab" not in l]
    if others:
        notes.append(f"{len(others)} llama-server process(es) running: "
                     + "; ".join(others[:2])[:160])
    # Deliberately NOT a preflight problem: MemAvailable is low precisely because
    # the router is up, and full mode stops it. Judging memory here would refuse
    # every real run. It is checked again after the stop, where it means something.
    avail = meminfo().get("MemAvailable", 0)
    notes.append(f"{human(avail)} available before the router is stopped "
                 f"(full mode needs {args.need_gb:.0f} GB after stopping it)")
    return binpath, models, problems, notes, model_notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="llama.cpp router TTL/OOM laboratory (runs detached, reports to disk)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["dry-run", "mini", "full"], default="dry-run")
    ap.add_argument("--dry-run", action="store_true",
                    help="shortcut for --mode dry-run (also the default)")
    ap.add_argument("--full", action="store_true", help="shortcut for --mode full")
    ap.add_argument("--mini", action="store_true", help="shortcut for --mode mini")
    ap.add_argument("--yes", action="store_true",
                    help="required for full: acknowledge the router goes down")
    ap.add_argument("--experiments", default="E0,E1,E2,E3,E4",
                    help="comma list, default all (" + ", ".join(EXPERIMENTS) + ")")
    ap.add_argument("--bin", help="llama-server to use (default: newest release)")
    ap.add_argument("--floor-gb", type=float, default=6.0,
                    help="guardian aborts below this MemAvailable (default 6)")
    ap.add_argument("--need-gb", type=float, default=40.0,
                    help="full mode aborts unless this much is free AFTER stopping "
                         "the router (default 40)")
    ap.add_argument("--allow-oom", action="store_true",
                    help="DISABLE the guardian. Not what you want.")
    ap.add_argument("--idle-secs", type=int, default=600)
    ap.add_argument("--cycles", type=int, default=2, help="E2 request rounds")
    ap.add_argument("--models-n", type=int, default=3, help="E4 model count")
    ap.add_argument("--fit-target", type=int, default=8192, help="MiB, E1")
    ap.add_argument("--sample-secs", type=float, default=1.0)
    ap.add_argument("--countdown", type=int, default=20,
                    help="seconds to abort a full run with Ctrl-C")
    ap.add_argument("--max-minutes", type=int, default=120)
    ap.add_argument("--restore-router-only", action="store_true")
    ap.add_argument("--background", action="store_true",
                    help="re-exec myself in a new session and return. This is what "
                         "makes it safe to start a full run from inside the agent "
                         "whose model the router is hosting: when the router stops, "
                         "the agent's context goes, but the run does not.")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()
    if args.full:
        args.mode = "full"
    if args.dry_run:
        args.mode = "dry-run"

    if args.background:
        # Detach before anything else exists: new session, no controlling terminal,
        # output to a bootstrap log. The parent returns immediately, so whoever
        # started it (a terminal, or an agent about to lose its model) is not the
        # process group the experiment runs in.
        LOGROOT.mkdir(parents=True, exist_ok=True)
        boot = LOGROOT / f"boot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.log"
        child_args = [a for a in sys.argv[1:] if a != "--background"]
        # Safe to let this raise: nothing has been touched yet — the router is
        # still up, no children exist, there is no state to unwind. A failure here
        # is "the experiment did not start", which is the correct and loud outcome.
        with open(boot, "ab") as fh:
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__)]
                                 + child_args, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        print(f"running detached as pid {p.pid}\nbootstrap log: {boot}\n"
              f"results when it finishes: {LOGROOT / 'latest' / 'summary.md'}\n"
              f"check on it: tail -f {boot}")
        return 0
    if args.mini:
        args.mode = "mini"
    args._argv = " ".join(sys.argv[1:])

    if args.list_models:
        listed, notes_l = find_models()
        for m in listed:
            print(f"{human(m['bytes']):>9}  {m['name']}"
                  + (f"  ({m['parts']} shards)" if m["parts"] > 1 else ""))
        for k, v in notes_l.items():
            if v:
                print(f"  {k}: {', '.join(v[:6])}")
        return 0

    lab = Lab(args)

    def on_signal(signum, _frame):
        # Any of these must land in the finally/atexit cleanup, because cleanup is
        # the only thing that puts the router back.
        lab.say(f"signal {signum} — cleaning up")
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, on_signal)
    for s in (signal.SIGTERM, signal.SIGQUIT, signal.SIGHUP, signal.SIGUSR1):
        try:
            signal.signal(s, on_signal)
        except (ValueError, OSError):
            pass
    atexit.register(lab.cleanup)

    # A run that was killed with SIGKILL never got to run cleanup, so the router
    # may still be down from it. Look for that before doing anything else.
    for stale in sorted(LOGROOT.glob("*/state.json")) if LOGROOT.is_dir() else []:
        try:
            info = json.loads(stale.read_text())
        except Exception:                                   # noqa: BLE001
            continue
        if info.get("stopped_router") and stale.parent != lab.dir:
            rc, st = run(["systemctl", "--user", "is-active", ROUTER_UNIT], timeout=30)
            if (st or "").strip() != "active":
                lab.say(f"previous run {stale.parent.name} left the router down — "
                        "restoring it first")
                lab._router_stopped_by_me = True
                lab.start_router()
            else:
                stale.unlink(missing_ok=True)
    try:                                 # best effort; needs privilege, so optional
        Path("/proc/self/oom_score_adj").write_text("-500")
        lab.say("parent oom_score_adj = -500")
    except OSError:
        lab.note("parent_oom_score_adj",
                 "could not lower (needs privilege) — children are at +900 instead")

    binpath, models, problems, notes, model_notes = preflight(args)
    if args.restore_router_only:
        lab._router_stopped_by_me = True
        lab.start_router()
        return 0
    if problems:
        for p in problems:
            lab.say("PREFLIGHT: " + p)
        if args.mode != "dry-run":
            lab.note("preflight_problems", problems)
            if args.mode == "full":
                lab.say("refusing to run a full experiment with preflight problems")
                return 2
    lab.results["bin"] = str(binpath)
    lab.results["models_seen"] = len(models)
    lab.note("model_notes", model_notes)
    for n in notes:
        lab.say("note: " + n)
    lab.results["preflight_notes"] = notes
    threading.Thread(target=lab._sampler, daemon=True).start()

    if args.mode == "dry-run":
        print(json.dumps({
            "mode": "dry-run",
            "bin": str(binpath),
            "mem_available_gb": round(meminfo().get("MemAvailable", 0) / GB, 1),
            "mem_note": "measured with the router up; full mode stops it first and "
                        f"requires {args.need_gb:.0f} GB free afterwards",
            "models": [{"name": m["name"], "gb": round(m["bytes"] / GB, 1),
                        "shards": m["parts"]} for m in models],
            "model_notes": model_notes,
            "will_stop_router": False,
            "experiments_if_full": [f"{k} {v[0]}" for k, v in EXPERIMENTS.items()],
            "guardian_floor_gb": args.floor_gb,
            "results_dir": str(lab.dir),
        }, indent=2))
        return 0

    if args.mode == "mini":
        # Validates the machinery — spawn, separation, sampling, guardian,
        # cleanup, reporting — on ~1 GB, with the production router left alone.
        lab.say("mini mode: the production router stays up; ~1 GB")
        small = next((m for m in models if m["bytes"] < 2 * GB), None)
        if not small or not binpath:
            lab.say("no small model to test with")
            return 2
        port = free_port()
        with lab.phase_ctx("MINI plumbing") as rec:
            farm = lab.dir / "farm-mini"
            write_farm(farm, [small["name"]], models)
            presets = lab.dir / "mini.ini"
            write_presets(presets, {"*": {"threads": 8, "fit": "on", "fit-target": 2048}})
            proc, logp = lab.spawn(
                [str(binpath), "--model", small["path"], "--host", "127.0.0.1",
                 "--port", str(port), "-c", "512", "--sleep-idle-seconds", "5",
                 "--no-webui"], "mini")
            rec["data"]["up"] = lab.wait_up(port, 120)
            t, code, _ = complete(lab, port, small["name"], tokens=4, timeout=300)
            rec["data"]["completion"] = f"http {code} in {t}s"
            time.sleep(12)
            lab.sample()
            rec["data"]["rss_gb"] = round(proc_mem(proc.pid).get("rss", 0) / GB, 2)
            rec["data"]["child_pid_alive"] = proc.poll() is None
            rec["data"]["own_session"] = (
                os.getpgid(proc.pid) != os.getpgid(os.getpid()))
            lab.events_from(logp)
            rec["data"]["events_seen"] = len(lab.events)
        lab.cleanup()
        print(f"\nresults: {lab.dir / 'summary.md'}")
        return 0

    # full
    if not args.yes:
        lab.say("--full stops ai-llama-router, which serves whatever is driving "
                "this. Add --yes when you mean it.")
        return 2
    lab.say(f"stopping the router in {args.countdown}s — Ctrl-C now if this "
            "was a mistake")
    time.sleep(args.countdown)
    lab.stop_router()
    free = meminfo().get("MemAvailable", 0)
    if free < args.need_gb * GB:
        # Something other than the router is holding the memory. Run the
        # experiment anyway and it becomes an experiment about that.
        lab.note("aborted_not_enough_memory",
                 f"{free / GB:.1f} GB free after stopping the router, need "
                 f"{args.need_gb:.0f} GB (--need-gb); nothing was run")
        lab.cleanup()
        return 3
    try:
        for key in [k.strip().upper() for k in args.experiments.split(",") if k.strip()]:
            if key not in EXPERIMENTS:
                lab.note(f"unknown_experiment_{key}", list(EXPERIMENTS))
                continue
            if lab.aborted:
                lab.note("stopped_early", lab.aborted)
                break
            if time.time() - lab.started > args.max_minutes * 60:
                lab.note("stopped_early", f"--max-minutes {args.max_minutes}")
                break
            _, fn = EXPERIMENTS[key]
            try:
                fn(lab, models, Path(binpath))
            except Exception as e:                          # noqa: BLE001
                lab.say(f"{key} error: {type(e).__name__}: {e}")
                lab.results["phases"].append({"name": key, "status": "error",
                                              "error": f"{type(e).__name__}: {e}"[:300],
                                              "data": {}})
    finally:
        lab.cleanup()
    restored = lab.results.get("router_restored") or {}
    print(f"\nresults: {lab.dir / 'summary.md'}")
    print(f"         {lab.dir / 'results.json'}")
    if restored:
        ok = restored.get("active") and restored.get("port_8080")
        print("router: " + ("back up and answering on :8080" if ok else
                            "NOT verified — check "
                            "`systemctl --user status ai-llama-router` and "
                            "`journalctl --user -u ai-llama-router -e`"))
    print("the agent that started this run lost its model mid-run; start it again "
          "and read the summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
