#!/usr/bin/env python3
"""Ask the running llama-server build which registry keys it accepts.

The portal's settings editor can write 30 keys into `presets.ini`. llama.cpp renames,
merges and drops server flags between builds. A key the build rejects does not fail
loudly: the worker drops it and the setting silently stops meaning anything. This tool
asks the build, key by key, and prints the answer.

Run it after every llama.cpp upgrade, before you trust a preset that names a new key.

    .venv/bin/python tools/registry_flag_audit.py                 # every key, smallest model
    .venv/bin/python tools/registry_flag_audit.py --keys kv-unified,parallel,fit-target
    .venv/bin/python tools/registry_flag_audit.py --build /home/cortana/ai/releases/llama.cpp-b11396/llama-server
    .venv/bin/python tools/registry_flag_audit.py --json /tmp/audit.json

It writes nothing to the portal's config. It starts its own servers on spare ports, each
in its own process group, and kills them. Point AI_PORTAL_HOME at a throwaway directory
to keep the portal's own state out of it.

Verdicts:
    accepts     the server started with the flag
    refuses     the server named the flag invalid and exited
    exit(N)     the server died for another reason (usually memory) — read the log
    timeout     the server never reported "listening" — read the log
    preset-only the key has no command-line form at all: a preset-only key, or a value the
                preset parser maps to a flag (`--kv-unified auto` is refused on the CLI and
                written as the bare flag from a preset). Measured by
                tools/spec_preset_probe.py, not here.

The probe model is the smallest *generative* cached file. An embedder is usually the
smallest file on a box, and loading one aborts in `llama_context::output_reserve` on
b11429 — every key would come back `exit(-6)`, which is a statement about the model.
"""
import argparse
import contextlib
import concurrent.futures
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.core import modelindex, modelmeta, paths             # noqa: E402
from app.core import personality as P                      # noqa: E402

TIMEOUT_SECONDS = 45
GIB = 1 << 30


# ---------------------------------------------------------------- the things under audit
def registry_rows():
    """Every key the settings editor can write, with its kind and section.

    The list comes from the portal's own registry, not a copy. If the registry grows,
    the audit covers the new key the same day it lands.
    """
    for key in sorted(P.SETTINGS):
        meta = P.SETTINGS[key]
        yield {"key": key, "kind": meta.get("kind"), "section": meta.get("section"),
               "default": meta.get("default"), "choices": list(meta.get("choices") or ()),
               "preset_only": bool(meta.get("preset_only")),
               "preset_mapped": bool(meta.get("preset_mapped"))}


def argv_for(key: str, meta: dict, value=None) -> list[str]:
    """The command-line form the portal's generator writes for this key.

    A bool key has no value: `--kv-unified` and `--no-kv-unified` are two flags, not one
    flag with an argument. Passing a value to a flag that takes none is what the router
    refuses, so the probe must use the same shape the generator uses.
    """
    kind = meta.get("kind")
    if value is None:
        value = meta.get("default")
    if kind == "bool":
        truthy = str(value).strip().lower() not in ("false", "off", "0", "no", "")
        return [f"--{key}"] if truthy else [f"--no-{key}"]
    if kind == "choice":
        return [f"--{key}"] + ([str(value)] if (meta.get("choices") or ()) else [])
    if kind in ("int", "float"):
        return [f"--{key}", str(value if value is not None else 1)]
    return [f"--{key}", str(value if value is not None else "x")]


# ---------------------------------------------------------------- running one probe
def smallest_cached_model(hub_cache: pathlib.Path) -> pathlib.Path | None:
    """The cheapest *generative* GGUF the box can already see.

    Two rules, both measured:
      - sidecars (mmproj) and stubs are skipped, as before;
      - pooled models are skipped. An embedder is the smallest file in most caches, and on
        b11429 loading one aborts in `llama_context::output_reserve` —
        `GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max)` — because its output count is
        the embedding width. The audit would report `exit(-6)` for every key, which says
        nothing about any flag. Use the portal's own index column (pooling_type), not a
        second naming rule.
    """
    best = None
    for p in hub_cache.rglob("*.gguf"):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        # The index's own rules, not a second copy of them: sidecars (mmproj, mtp-,
        # draft-, ...) and shards are not models. Loading an MTP draft as a model
        # segfaults (rc=-11, measured on b11429), and a probe that dies says nothing
        # about the flag it was started with.
        # NOT_A_MODEL_MARKERS is a substring test and covers every prefix _is_aux
        # matches, so one rule does the naming; _shard_of covers multi-part files.
        if modelindex._shard_of(p.name):
            continue
        if any(m in p.name.lower() for m in modelindex.NOT_A_MODEL_MARKERS):
            continue
        if size < 10 * 1024 * 1024:
            continue
        meta = modelmeta.sniff_gguf(p)
        if (meta or {}).get("pooling_type"):
            continue
        if best is None or size < best[0]:
            best = (size, p)
    return best[1] if best else None


def mem_available() -> int:
    """Bytes the kernel says are free to use. Same gauge the bench tool prices against."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return 0
    return 0


def free_port() -> int:
    """Ask the kernel for a free port instead of guessing one."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_one(binary: str, model: pathlib.Path, row: dict, work: pathlib.Path) -> dict:
    """Start the server with this key's flag and report what the build said."""
    # A preset-only key never reaches a command line, and a value-mapped key is mapped by
    # the preset parser: `--kv-unified auto` is refused on the CLI while the preset writes
    # the bare flag. Probing either on the command line measures the probe, not the build.
    if row.get("preset_only") or row.get("preset_mapped"):
        return {"key": row["key"], "kind": row["kind"], "section": row["section"],
                "argv": [], "verdict": "preset-only",
                "why": "no command-line form; probe it through a preset "
                       "(tools/spec_preset_probe.py)", "log": ""}

    argv = argv_for(row["key"], row)
    port = free_port()
    log = work / f"{row['key']}.log"
    cmd = [binary, "--model", str(model), "--offline", "--host", "127.0.0.1",
           "--port", str(port), "-ngl", "0", "--ctx-size", "1024", "--parallel", "1"] + argv
    rec = {"key": row["key"], "kind": row["kind"], "section": row["section"],
           "argv": argv[1:], "verdict": "timeout", "why": ""}
    try:
        with open(log, "wb") as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    preexec_fn=os.setsid)
    except OSError as exc:
        rec["verdict"], rec["why"] = "spawn-failed", str(exc)[:160]
        return rec
    for _ in range(TIMEOUT_SECONDS):
        time.sleep(1)
        text = log.read_text(errors="replace")
        low = text.lower()
        if "invalid argument" in low:
            line = next((ln for ln in text.splitlines() if "invalid" in ln.lower()), "")
            rec["verdict"], rec["why"] = "refuses", line.strip()[:160]
            break
        if "listening on" in low:
            rec["verdict"] = "accepts"
            break
        if proc.poll() is not None:
            rec["verdict"] = "exit"
            rec["why"] = (f"rc={proc.returncode} — read the log "
                          f"(an oversized model exits here too)")
            break
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    rec["log"] = str(log)
    return rec


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Ask llama-server which registry keys it accepts.")
    ap.add_argument("--build", help="llama-server binary to audit (default: AI_LLAMA_SERVER "
                                    "or ~/ai/bin/llama-server)")
    ap.add_argument("--model", help="GGUF to load for the probe (default: smallest cached file)")
    ap.add_argument("--keys", help="comma-separated subset of registry keys")
    ap.add_argument("--list", action="store_true", help="print the keys and exit")
    ap.add_argument("--concurrency", type=int, default=4, help="probes in flight (default 4)")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--work", help="probe log directory (default: a fresh /tmp dir)")
    ap.add_argument("--hub-cache", default=str(paths.HF_CACHE), help="hub cache to shop for a model")
    ap.add_argument("--min-mem-gib", type=float, default=8.0,
                    help="refuse to start probes below this much free RAM")
    args = ap.parse_args()

    rows = list(registry_rows())
    if args.keys:
        wanted = {k.strip() for k in args.keys.split(",") if k.strip()}
        unknown = sorted(wanted - {r["key"] for r in rows})
        if unknown:
            print(f"not in the registry: {', '.join(unknown)}")
            return 2
        rows = [r for r in rows if r["key"] in wanted]
    if args.list:
        for r in rows:
            print(f"{r['key']:<26} {r['kind'] or '':<7} {r['section'] or ''}")
        return 0

    binary = args.build or os.environ.get("AI_LLAMA_SERVER") or str(paths.AI_BIN / "llama-server")
    if not pathlib.Path(binary).is_file():
        print(f"no llama-server at {binary} — pass --build")
        return 2
    model = pathlib.Path(args.model) if args.model else smallest_cached_model(pathlib.Path(args.hub_cache))
    if not model or not model.is_file():
        print("no GGUF to probe with — pass --model /path/to/file.gguf")
        return 2

    mem_free = mem_available() // GIB
    if mem_free < args.min_mem_gib:
        print(f"MemAvailable {mem_free} GiB is under the floor ({args.min_mem_gib}) — free RAM first")
        return 2

    work = pathlib.Path(args.work) if args.work else pathlib.Path(
        tempfile.mkdtemp(prefix="registry-audit-"))
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot use {work}: {exc}")
        return 2

    print(f"build: {binary}")
    print(f"model: {model.name} ({model.stat().st_size / GIB:.2f} GiB)")
    print(f"keys:  {len(rows)}   MemAvailable {mem_free} GiB   logs: {work}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(run_one, binary, model, r, work) for r in rows]
        for f in concurrent.futures.as_completed(futures):
            rec = f.result()
            results.append(rec)
            print(f"  {rec['key']:<26} {rec['verdict']:<8} {' '.join(rec['argv'])}"
                  f"{'  <- ' + rec['why'] if rec['why'] else ''}")

    results.sort(key=lambda r: r["key"])
    # A declined key is not a finding: it has no command-line form to accept. Counting it
    # as a refusal makes a clean audit exit 1, and a nightly run then reports four defects
    # that were measured away months ago.
    refuses = [r for r in results if r["verdict"] == "refuses"]
    declined = [r for r in results if r["verdict"] == "preset-only"]
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(
            {"build": binary, "model": str(model), "mem_available_gib": mem_free,
             "keys": len(results), "results": results}, indent=1))
        print(f"\nreport: {args.json}")
    print(f"\naccepted {len(results) - len(refuses) - len(declined)} of {len(results)}"
          f" ({len(declined)} declined: no command-line form)")
    if declined:
        print("keys with no command-line form (measured by tools/spec_preset_probe.py):")
        for r in declined:
            print(f"  {r['key']:<26} {r['why'][:80]}")
    if refuses:
        print("keys the build did not accept:")
        for r in refuses:
            print(f"  {r['key']:<26} {r['verdict']:<8} {r['why'][:90]}")
        print("\nA refused key is a portal setting that silently does nothing on this build.")
        print("docs/PORTAL_PLAN.md sec 6.12.21 records how to fix a registry entry after an audit.")
    return 1 if refuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
