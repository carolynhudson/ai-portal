#!/usr/bin/env python3
"""fit_probe.py — does llama.cpp's `fit` account for memory used by *others*?

Answer, corrected 2026-09-24: yes, for memory held *on the device*. This probe held its hog in host
RAM, which a device-memory gauge cannot see, and concluded "blind". `tools/fit_awareness_probe.py`
re-asks the same question with llama.cpp processes as the residents and shows `fit` measuring free
device memory and reducing the context. Keep this file as the record of what was measured; read
§6.12.7 for what it turned out to mean.

Why this exists separately from ttl_oom_lab
-------------------------------------------
ttl_oom_lab answers "what does the router do with 150 GB of demand", which needs
the production router stopped and kills the agent that starts it. That is the
expensive way to answer a question that does not require 150 GB at all.

The actual question is narrow: when a worker starts with `fit = on` and
`fit-target = N`, does it measure free memory as

    (a) the whole machine, ignoring what other processes already hold, or
    (b) what is genuinely left after everyone else's RSS?

If (a), two models can each individually "fit" and jointly destroy the box — the
failure the portal's §4.7 budget exists to prevent. If (b), the router is
self-defending and the budget is a convenience.

How it answers it cheaply
-------------------------
`fit` is a *startup* decision. It happens once, when the worker comes up, and it
sets the context size from whatever memory it believes is free. So we do not need
big models — we need a controlled amount of *non-model* memory held by a
third-party process, and a small model whose fit-decision we can read out.

    1. start worker A with fit-target T on a small model, nothing else loaded,
       and read the context size it chose           -> baseline
    2. allocate R GB of anonymous memory in a helper process (a stand-in for a
       second model's weights: anonymous, unreclaimable, exactly like GGUF
       weights as measured in §6.12)
    3. start worker B, same model, same fit-target
       - if B picks the SAME context as A -> fit ignored the R GB. It is blind.
       - if B picks a SMALLER context     -> fit saw it. It accounts for others.
    4. optionally raise R until B refuses to start, to find the threshold.

R is held by a helper we control, so the experiment is bounded, fast, and safe:
no model anywhere near the size of memory, no router restart, no agent death.

Run:  python3 tools/fit_probe.py --help
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

AI = Path.home() / "ai"
GB = 1 << 30
MiB = 1 << 20


def mem_avail() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except Exception as e:                                     # noqa: BLE001
        return 125, f"{type(e).__name__}: {e}"


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(port: int, path: str, payload=None, timeout=20):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace") if e.fp else str(e))
    except Exception as e:                                     # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


class Hog:
    """Hold R GB of anonymous memory, like a second model's weights would.

    Touched page by page so it is genuinely resident, not merely reserved — the
    whole point of §6.12 is that GGUF weights are anonymous resident pages, and a
    reservation the kernel has not committed would not be visible to `fit` either.
    """

    def __init__(self, gb: float):
        self.gb = gb
        self.proc = None

    def start(self):
        # int() of a float is total here; --hog is parsed by argparse as float.
        # pi-lens-ignore: unchecked-throwing-call-python
        pages = int(self.gb * 1024)                      # 1 MiB pages
        code = (
            "import mmap,sys\n"
            f"n={pages}*1024*1024\n"
            "m=mmap.mmap(-1,n)\n"
            "for i in range(0,n,4096): m[i]=65\n"      # touch every page
            "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
            "import time\n"
            "time.sleep(3600)\n"
        )
        self.proc = subprocess.Popen([sys.executable, "-c", code],
                                     stdout=subprocess.PIPE, text=True,
                                     start_new_session=True)
        got = ""
        try:
            # stdout is not None here (PIPE above), but a failed spawn must return
            # "did not start", not raise AttributeError from inside the probe.
            if self.proc.stdout is not None:
                got = self.proc.stdout.readline().strip()
        except Exception:                                    # noqa: BLE001
            got = ""
        ok = got == "held"
        try:
            Path(f"/proc/{self.proc.pid}/oom_score_adj").write_text("900")
        except OSError:
            pass
        return ok

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except Exception:                                # noqa: BLE001
                pass


def worker_up(port: int, timeout=180) -> bool:
    """True only when the worker can actually generate.

    The first version treated any answer (200/400/404/412/503) as up, which is
    wrong in the one direction that matters here: a server that has bound its
    port but is still loading — or is dying — answers, so every run looked
    healthy and the fit experiment measured nothing. Readiness for this probe is
    'it produced a token', because that is the thing the experiments are about.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, body = http_json(port, "/completion",
                               {"prompt": "hi", "n_predict": 1, "stream": False},
                               timeout=15)
        if code == 200 and "content" in body:
            return True
        time.sleep(2)
    return False


def chosen_ctx(log: Path) -> dict:
    """Pull the numbers `fit` decided on out of the worker's own log.

    llama.cpp prints the context it settled on and the buffer sizes it
    allocated; those are the observable output of the fit decision, which is
    what we are actually comparing between runs.
    """
    out = {"n_ctx": None, "n_ctx_slot": None, "ctx_size_lines": [],
           "fit_lines": []}
    try:
        txt = log.read_text(errors="replace")
    except OSError:
        return out
    for line in txt.splitlines():
        if re.search(r"n_ctx\b|context size|n_ctx_seq|size =\s*\d+", line, re.I):
            out["ctx_size_lines"].append(line.strip()[:180])
        if re.search(r"\bfit\b|fit_params|estimated|measured|KV cache|memory",
                     line, re.I):
            out["fit_lines"].append(line.strip()[:180])
    # The number that matters is n_ctx_slot: it is what the server settles on
    # AFTER fit has had its say, and it is the only field printed at the default
    # verbosity. Matching bare `n_ctx` matched nothing and made a clean result
    # report as inconclusive.
    for key in ("n_ctx_slot", "n_ctx"):
        m = re.findall(rf"\b{key}\s*=\s*(\d+)", txt)
        if m:
            # pi-lens-ignore: unchecked-throwing-call-python
            out[key] = int(m[-1])
            if key == "n_ctx_slot":
                out["n_ctx"] = out[key]
    return out


def one_run(label: str, model: Path, binpath: Path, fit_target_mib: int,
            ctx_max: int, hog_gb: float, log: Path) -> dict:
    """Start one worker with an optional hog in place; report what fit chose."""
    port = free_port()
    hog = Hog(hog_gb) if hog_gb > 0 else None
    info = {"label": label, "hog_gb": hog_gb, "fit_target_mib": fit_target_mib,
            "free_before_gb": round(mem_avail() / GB, 2), "started": False,
            "up": False}
    try:
        if hog:
            if not hog.start():
                info["error"] = f"could not hold {hog_gb} GB"
                return info
            info["free_with_hog_gb"] = round(mem_avail() / GB, 2)
        cmd = [str(binpath), "--model", str(model), "--host", "127.0.0.1",
               "--port", str(port), "--no-webui", "--fit", "on",
               "--fit-target", str(fit_target_mib), "-c", str(ctx_max)]
        with open(log, "w") as fh:
            fh.write(f"===== {label} hog={hog_gb}GB\n")
            fh.flush()
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            Path(f"/proc/{p.pid}/oom_score_adj").write_text("900")
        except OSError:
            pass
        info["started"] = True
        info["up"] = worker_up(port, 180)
        if not info["up"]:
            rc = p.poll()
            info["exit_code"] = rc
            info["died"] = rc is not None
        p.terminate()
        try:
            p.wait(timeout=30)
        except Exception:                                    # noqa: BLE001
            p.kill()
        time.sleep(2)
        info.update(chosen_ctx(log))
    finally:
        if hog:
            hog.stop()
        time.sleep(2)
    return info


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Does llama.cpp's fit account for memory held by others?",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="a small .gguf (default: smallest in library)")
    ap.add_argument("--bin", help="llama-server (default: newest release)")
    ap.add_argument("--fit-target", type=int, default=4096, dest="fit_target",
                    help="MiB, as in presets.ini (default 4096)")
    ap.add_argument("--ctx", type=int, default=8192,
                    help="max context to request; fit must shrink it (default 8192)")
    ap.add_argument("--hog", type=float, default=12.0,
                    help="GB of anonymous memory to hold in round 2 (default 12)")
    ap.add_argument("--out", help="write JSON here")
    args = ap.parse_args()

    def build_of(p: Path) -> int:
        # Total: an unparseable release dir sorts lowest instead of raising and
        # killing the probe before it has measured anything. \d+ cannot fail int().
        m = re.search(r"-b(\d+)$", p.parent.name)
        # pi-lens-ignore: unchecked-throwing-call-python
        return int(m.group(1)) if m else -1
    bins = sorted((AI / "releases").glob("llama.cpp-b*/llama-server"), key=build_of)
    binpath = Path(args.bin) if args.bin else (bins[-1] if bins else None)
    if not binpath or not binpath.is_file():
        print("no llama-server found", file=sys.stderr)
        return 2
    model = Path(args.model) if args.model else None
    if not model:
        cands = sorted((AI / "models" / "llama").rglob("*.gguf"),
                       key=lambda p: p.stat().st_size if p.exists() else 1 << 62)
        cands = [p for p in cands
                 if p.exists() and not re.match(
                     r"(mmproj|mtp|draft|imatrix|adapter)-", p.name, re.I)
                 and 20 * MiB < p.stat().st_size < 2 * GB]
        if not cands:
            print("no small model found", file=sys.stderr)
            return 2
        model = cands[0]

    print(f"bin   : {binpath}")
    print(f"model : {model} ({model.stat().st_size / MiB:.0f} MiB)")
    print(f"fit   : on, target {args.fit_target} MiB | requested ctx {args.ctx}")
    print(f"free  : {mem_avail() / GB:.1f} GB")
    print()

    outdir = Path(args.out).parent if args.out else Path("/tmp/fit-probe")
    outdir.mkdir(parents=True, exist_ok=True)

    base = one_run("baseline (no hog)", model, binpath, args.fit_target,
                   args.ctx, 0.0, outdir / "baseline.log")
    hogged = one_run(f"hog {args.hog} GB", model, binpath, args.fit_target,
                     args.ctx, args.hog, outdir / "hogged.log")

    print(json.dumps(base, indent=2)[:1200])
    print(json.dumps(hogged, indent=2)[:1200])

    print("\n=== verdict")
    b_ctx = base.get("n_ctx_slot") or base.get("n_ctx")
    h_ctx = hogged.get("n_ctx_slot") or hogged.get("n_ctx")
    print(f"  context chosen, no hog : {b_ctx}")
    print(f"  context chosen, {args.hog} GB held : {h_ctx}")
    if b_ctx is None or h_ctx is None:
        print("  INCONCLUSIVE: could not read the chosen context from one of the "
              "runs. Inspect /tmp/fit-probe/*.log — grep for 'n_ctx'.")
        verdict = "inconclusive"
    elif h_ctx == b_ctx:
        print(f"  BLIND: fit chose the same context with {args.hog} GB already "
              "held by another process.")
        print("  => two models can each individually fit and jointly OOM. The")
        print("     portal-side budget (§4.7) is MANDATORY, not advisory.")
        verdict = "blind"
    elif h_ctx < b_ctx:
        print(f"  AWARE: fit shrank the context from {b_ctx} to {h_ctx} when "
              f"{args.hog} GB was already held.")
        print("  => the worker accounts for other residents at startup.")
        verdict = "aware"
    else:
        print("  SURPRISING: context grew while memory was held. Re-check.")
        verdict = "unexpected"

    result = {"verdict": verdict, "baseline": base, "hogged": hogged,
              "fit_target_mib": args.fit_target, "requested_ctx": args.ctx,
              "hog_gb": args.hog, "model": str(model), "bin": str(binpath)}
    out = Path(args.out) if args.out else Path("/tmp/fit-probe/result.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
