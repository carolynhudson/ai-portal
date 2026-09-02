#!/usr/bin/env python3
"""Phase 4 — router cutover.  *** USER-RUN, from a separate terminal ***

Stops the currently running router (which is serving the AI agent session —
that session will drop, that's expected), moves the old flat model dir into
~/ai/state/rollback/, installs a systemd user service pointing at the new
layout, and verifies the new model list.

  new binary    ~/ai/bin/llama-server            (phase 3 release)
  new model dir ~/ai/models/llama                (phase 1, one subdir per model)
  new presets   ~/ai/config/llama/presets.ini    (phase 1, validated keys)
  port/host     8080 / 127.0.0.1                 (unchanged)

Rollback (everything is preserved):
  systemctl --user disable --now ai-llama-router
  mv ~/ai/state/rollback/models-flat-<ts> ~/.cache/llama.cpp/models
  <old cmdline, saved in ~/ai/state/rollback/phase4-<ts>/old-cmdline>

Usage:
  python3 phase4_router_cutover.py --dry-run   # pre-flight only, kills nothing
  python3 phase4_router_cutover.py             # the real cutover
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import aiportal as ap

SERVICE_NAME = "ai-llama-router"
UNIT = f"""[Unit]
Description=llama.cpp model router (AI Portal)
After=network.target

[Service]
ExecStart={ap.AI_BIN}/llama-server --host {ap.ROUTER_HOST} --port {ap.ROUTER_PORT} \\
    --models-max 1 \\
    --models-dir {ap.LLAMA_MODELS} \\
    --models-preset {ap.PRESETS_INI}
Environment=HF_HOME={ap.HF_CACHE}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

EXPECTED_MODELS = [
    "Laguna-S-2.1-Q4_K_M",
    "Mistral-Code-Architect-7b",
    "Qwen3-Coder-Next-Q4_K_M",
    "Qwen3.5-0.8B.Q4_K_M",
    "Qwen3.6-35B-A3B-Q4_K_M",
    "Qwen3.8-27B-UD-Q8_K_XL",
    "Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M",
    "ThinkingCap-Qwen3.6-27B-Q8_0",
    "gemma-4-31B-it-QAT-Q4_0",
    "muse-glimmer-30B-kquant-17gb",
    "ornith-1.0-35b-Q5_K_M",
    "qwen3-emb-8b-Q8_0",
    "stackexchange_literature.Q8_0",
]

def llama_pids() -> list[tuple[int, str]]:
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except OSError:
            continue
        if "llama-server" in cmd and "phase4" not in cmd:
            out.append((int(pid), cmd))
    return out


def preflight() -> None:
    ap.log("preflight...")
    exe = ap.AI_BIN / "llama-server"
    if not exe.exists():
        ap.die(f"{exe} missing — run phase 3 first")
    v = ap.run([str(exe), "--version"]).splitlines()[0]
    ap.log(f"  release binary: {v}")
    ldd = ap.run(["ldd", str(exe)])
    if "not found" in ldd:
        ap.die("release binary has unresolved libs")
    subs = sorted(p.name for p in ap.LLAMA_MODELS.iterdir() if p.is_dir())
    missing = [m for m in EXPECTED_MODELS if m not in subs]
    if missing:
        ap.die(f"model dirs missing: {missing}")
    for p in ap.LLAMA_MODELS.iterdir():
        if p.is_dir() and not list(p.glob("*.gguf")):
            ap.die(f"model dir {p.name} has no .gguf")
    if not ap.PRESETS_INI.exists():
        ap.die("presets.ini missing — run phase 1 first")
    pids = llama_pids()
    if not pids:
        ap.log("  NOTE: no llama-server process found — the router may already be down.")
    else:
        for pid, cmd in pids:
            ap.log(f"  running: pid {pid}: {cmd[:120]}")
    ap.log("preflight OK")


def save_rollback() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rb = ap.ROLLBACK_DIR / f"phase4-{ts}"
    rb.mkdir(parents=True, exist_ok=True)
    for pid, cmd in llama_pids():
        (rb / f"old-cmdline-{pid}").write_text(cmd + "\n")
    (rb / "README.txt").write_text(
        f"phase4 cutover rollback, {ts}\n"
        f"1) systemctl --user disable --now {SERVICE_NAME}\n"
        f"2) mv {ap.ROLLBACK_DIR}/models-flat-{ts} {ap.LEGACY_LLAMA_MODELS}\n"
        f"3) re-run the old cmdline saved in old-cmdline-* (terminal, foreground)\n")
    ap.log(f"rollback info saved to {rb}")
    return rb


def stop_router() -> None:
    pids = [pid for pid, _ in llama_pids()]
    if not pids:
        ap.log("no llama-server processes running")
        return
    ap.log(f"stopping llama-server processes: {pids}")
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not llama_pids():
            break
        time.sleep(1)
    for pid, _ in llama_pids():
        ap.warn(f"pid {pid} did not exit, SIGKILL")
        os.kill(pid, signal.SIGKILL)
    time.sleep(1)
    ap.log("router stopped")


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--dry-run", action="store_true")
    args = ap_.parse_args()
    dry = args.dry_run

    preflight()
    if dry:
        ap.log("dry run: no changes made")
        return

    rb = save_rollback()
    ts = rb.name.split("-")[-1]

    # move old flat model dir aside (kept for rollback; deleted in phase 5)
    if ap.LEGACY_LLAMA_MODELS.exists():
        old = ap.ROLLBACK_DIR / f"models-flat-{ts}"
        ap.log(f"moving {ap.LEGACY_LLAMA_MODELS} -> {old}")
        shutil.move(str(ap.LEGACY_LLAMA_MODELS), str(old))

    stop_router()

    # install + start the user service
    unit_path = Path(os.path.expanduser("~/.config/systemd/user")) / f"{SERVICE_NAME}.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(UNIT)
    ap.log(f"wrote {unit_path}")
    ap.run(["systemctl", "--user", "daemon-reload"])
    # keep the user session alive after logout so the router keeps running
    ap.run(["loginctl", "enable-linger", os.environ.get("USER", "")], dry=False)
    ap.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
    time.sleep(3)
    ap.run(["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"])

    # verify the new model list
    ap.log("verifying /v1/models ...")
    import urllib.request
    data = None
    for attempt in range(10):
        try:
            data = json.load(urllib.request.urlopen(
                f"http://{ap.ROUTER_HOST}:{ap.ROUTER_PORT}/v1/models", timeout=5))
            break
        except Exception as e:  # noqa: BLE001
            ap.warn(f"router not answering yet ({e}); retry {attempt + 1}/10")
            time.sleep(3)
    else:
        ap.die("router did not come up on port 8080 — check: systemctl --user status " + SERVICE_NAME)
    if data is None:
        ap.die("router did not answer")

    models = {m["id"]: m for m in data.get("data", [])}

    def preset_text(mid: str) -> str:
        return models.get(mid, {}).get("status", {}).get("preset", "")

    ap.log(f"router lists {len(models)} models:")
    ok = True
    for mid in sorted(models):
        st = models[mid].get("status", {}).get("value", "?")
        ap.log(f"  {mid:45s} {st}")
    for m in EXPECTED_MODELS:
        if m not in models:
            ap.warn(f"MISSING expected model: {m}")
            ok = False
    shard = [m for m in models if "-0000" in m]
    if shard:
        ap.warn(f"shard-only model ids still present (F2 not fixed): {shard}")
        ok = False
    if "00001-of-00002" not in preset_text("Laguna-S-2.1-Q4_K_M"):
        ap.warn("Laguna preset does not reference shard 00001 — check subdirectory scan")
        ok = False
    if "model-draft" not in preset_text("Qwen3.8-27B-UD-Q8_K_XL"):
        ap.warn("Qwen3.8 preset has no model-draft — MTP draft not attached (check mtp-*.gguf in its dir)")
        ok = False
    for vm in ("gemma-4-31B-it-QAT-Q4_0", "ThinkingCap-Qwen3.6-27B-Q8_0",
               "Qwen3.6-35B-A3B-Q4_K_M", "Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M",
               "muse-glimmer-30B-kquant-17gb"):
        if "mmproj" not in preset_text(vm):
            ap.warn(f"{vm} preset has no mmproj — vision sidecar not attached")
            ok = False

    if ok:
        ap.log("CUTOVER VERIFIED: all 13 models present, no shard-only ids, "
               "Laguna single entry, mmproj + MTP draft attached")
    else:
        ap.log("cutover finished but VERIFICATION ISSUES FOUND — see warnings above.")
        ap.log(f"rollback: {rb}/README.txt")
    ap.log("NOTE: if you are talking to the agent through this router, the old "
           "session has ended; start a new one. The new router is the systemd "
           f"user service {SERVICE_NAME}.")


if __name__ == "__main__":
    main()
