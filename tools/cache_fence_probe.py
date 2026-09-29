"""What does the router actually get to load — the directory fence, or the cache too?

Run it. It answers three questions against the installed llama-server, without ever
loading a model (no GPU is allocated, no aperture is pinned):

  1. does `--models-dir` restrict what the router can see?
  2. does a `HF_HUB_CACHE` redirect empty the hub-cache view while leaving the
     `--models-dir` entries alone?
  3. what does the runtime itself say the cache holds (`--cache-list`)?

Why this exists: §4.7 defines a personality's model set as a symlink farm and claims
everything outside it "cannot be loaded, cannot be OOM'd by". Measured on b11146 that
is true of the directory source and false of the Hugging Face cache — a router whose
`--models-dir` held nothing still listed five cache repos, and `POST /models/load`
for one of them returned `{"success": true}` and started a worker. So the budget
guarantee ("the heaviest models_max combination must fit") is conditional on the
loadable set being closed, and the way to close it is the env, not the farm. That
load experiment is deliberately NOT reproduced here: it pins aperture. Do it by hand
on a box you are watching if you want the number.

Usage:  python tools/cache_fence_probe.py [--models-dir DIR] [--port N]
"""
from __future__ import annotations

import argparse
import itertools
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

PORTS = itertools.count(8131)
# the operator() line is prefixed with a timestamp and log tags, so search
# rather than anchor: "0.00.209.621 I srv    operator():   [models_dir] name"
LISTED = re.compile(r"\[\s*(cache|models_dir)\]\s+(\S+)")
COUNT = re.compile(r"Available models \((\d+)\)")


def free_port() -> int:
    import socket
    while True:
        p = next(PORTS)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue


def listing(binary: pathlib.Path, *, models_dir: pathlib.Path | None,
            hub: pathlib.Path | None, label: str) -> None:
    """Start a router, read its own model listing, kill it. Never loads anything."""
    env = dict(os.environ)
    if hub is not None:
        env["HF_HUB_CACHE"] = str(hub)
    else:
        env.pop("HF_HUB_CACHE", None)
    args = [str(binary), "--host", "127.0.0.1", "--port", str(free_port()),
            "--no-models-autoload", "--models-max", "1"]
    if models_dir is not None:
        args += ["--models-dir", str(models_dir)]
    port = args[args.index("--port") + 1]
    log = pathlib.Path("/tmp/cache_fence_%s.log" % label.replace(" ", "_"))
    log.unlink(missing_ok=True)
    p = subprocess.Popen(args, env=env, stdout=log.open("w"),
                         stderr=subprocess.STDOUT, start_new_session=True)
    rows: list[str] = []
    n: int | None = None
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1.5)
            text = log.read_text(errors="replace") if log.exists() else ""
            if "Available models" in text:
                m = COUNT.search(text)
                n = int(m.group(1)) if m else n
                rows = [f"  [{mm.group(1)}] {mm.group(2)}"
                        for mm in (LISTED.search(line) for line in text.splitlines())
                        if mm]
                break
    finally:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait(timeout=20)
    print(f"\n=== {label}: {n if n is not None else '?'} model(s) visible")
    for r in rows[:40]:
        print(r)
    if not rows:
        print(f"  (nothing listed — see {log})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=os.environ.get("AI_LLAMA_SERVER")
                    or shutil.which("llama-server")
                    or "/home/cortana/ai/bin/llama-server")
    ap.add_argument("--models-dir", default="/tmp/cache_fence_empty_dir")
    args = ap.parse_args()

    binary = pathlib.Path(args.binary)
    if not binary.exists():
        print(f"no llama-server at {binary}", file=sys.stderr)
        return 2
    mdir = pathlib.Path(args.models_dir)
    mdir.mkdir(parents=True, exist_ok=True)
    empty_hub = pathlib.Path("/tmp/cache_fence_empty_hub")
    empty_hub.mkdir(parents=True, exist_ok=True)

    print(f"binary: {binary}")
    print(f"--models-dir: {mdir} ({len(list(mdir.iterdir()))} entries)")
    print(f"empty hub for the redirect: {empty_hub}")

    listing(binary, models_dir=mdir, hub=None, label="as-configured")
    listing(binary, models_dir=mdir, hub=empty_hub, label="HF_HUB_CACHE fenced")

    for env_label, hub in (("inherited env", None), ("fenced", empty_hub)):
        env = dict(os.environ)
        if hub is not None:
            env["HF_HUB_CACHE"] = str(hub)
        else:
            env.pop("HF_HUB_CACHE", None)
        out = subprocess.run([str(binary), "--cache-list"], env=env,
                             capture_output=True, text=True, timeout=120)
        text = out.stdout + out.stderr
        n = next((l.strip() for l in text.splitlines() if "models in cache" in l), "?")
        print(f"\n=== --cache-list ({env_label}): {n}")
        for line in text.splitlines():
            m = re.match(r"^\s*\d+\.\s+(\S+)\s*$", line)
            if m:
                print(f"  {m.group(1)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
