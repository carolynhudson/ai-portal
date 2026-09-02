#!/usr/bin/env python3
"""Phase 5 — cleanup of superseded/duplicate data.  USER-RUN.

Every action is behind an explicit flag; without --yes nothing is touched
(dry-run prints the actions instead).

  --after-cutover     remove the old flat model dir (moved aside by phase 4),
                      all stale ~/.llama.cpp.build.* staging trees, the stale
                      clone in ~/.local/share/llama-cpp, and llama
                      binaries/libs/tests from ~/.local/bin (keeps hf/env).
                      Adds ~/ai/bin to PATH in ~/.bashrc.
  --comfy-merge-done  remove the stale workspace ComfyUI clone (hash-verifies
                      its 2 unique models exist in ~/ai/comfyui/models first).
  --remove-lmstudio   hash-verify every GGUF in ~/models against the canonical
                      store, then MOVE ~/models into state/rollback (frees
                      nothing yet — delete the rollback copy when confident).
  --archive-scripts   move prior-AI scripts/logs from ~ into
                      ~/ai-projects/ai-portal/archive/legacy/
  --remove-deb        remove ~/docker-desktop-amd64.deb (433 M)

  --yes               actually execute (default: dry-run)

Examples:
  python3 phase5_cleanup.py --after-cutover --comfy-merge-done          # preview
  python3 phase5_cleanup.py --yes --after-cutover --comfy-merge-done \
      --remove-deb --archive-scripts
  python3 phase5_cleanup.py --yes --remove-lmstudio   # after living with it a while
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import aiportal as ap

KEEP_IN_LOCAL_BIN = {"hf", "env", "env.fish"}

HOME_SCRIPTS_AI = [
    "00-cleanup-shell.sh",
    "01-setup-prerequisites.sh",
    "02-fix-llama-cpp.sh",
    "02-5-test-llama-cpp.sh",
    "02-install-llama-fresh.sh",
    "03-migrate-models.sh",
    "backup-before-cleanup.sh",
    "diagnose-llama.sh",
    "discover-models.py",
    "model_migration_20260825_165214.log",
    "ai_environment_report_20260825_123454.txt",
    "duck.ai_2026-08-25_13-25-14.txt",
]
DOWNLOADS_JUNK = ["duck.ai_2026-08-25_13-25-14.txt"]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def act(msg: str, fn, dry: bool) -> None:
    if dry:
        ap.log(f"would: {msg}")
    else:
        ap.log(msg)
        fn()


def do_after_cutover(dry: bool) -> None:
    # 1. old flat model dir(s) moved aside by phase 4
    for old in sorted(ap.ROLLBACK_DIR.glob("models-flat-*")):
        size = sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
        act(f"remove old flat model dir {old} ({ap.human(size)} of links)",
            lambda d=old: shutil.rmtree(d, ignore_errors=True), dry)
    # 2. stale staging build trees (F3/F7) — safe only after phase 3+4
    for st in sorted(ap.HOME.glob(".llama.cpp.build.*")):
        if st.is_dir():
            act(f"remove staging tree {st}",
                lambda d=st: shutil.rmtree(d, ignore_errors=True), dry)
    # 3. stale second clone
    if ap.LEGACY_LLAMA_SHARE.is_dir():
        act(f"remove stale clone {ap.LEGACY_LLAMA_SHARE}",
            lambda: shutil.rmtree(ap.LEGACY_LLAMA_SHARE, ignore_errors=True), dry)
    # 4. ~/.local/bin: drop llama artifacts, keep hf/env
    victims = []
    if ap.LEGACY_LOCAL_BIN.is_dir():
        for p in ap.LEGACY_LOCAL_BIN.iterdir():
            if p.name in KEEP_IN_LOCAL_BIN:
                continue
            if re.match(r"^(llama|libllama|libggml|libmtmd|test-)", p.name):
                victims.append(p)
    for p in victims:
        act(f"remove {p}", lambda x=p: x.unlink(missing_ok=True), dry)
    # 5. PATH for ~/ai/bin
    bashrc = ap.HOME / ".bashrc"
    if bashrc.exists() and "ai/bin" not in bashrc.read_text():
        content = bashrc.read_text()
        marker = 'export PATH="$HOME/.local/bin:$PATH"'
        if marker in content:
            new = content.replace(marker,
                                  'export PATH="$HOME/ai/bin:$PATH"  # ai-portal\n' + marker, 1)
        else:
            new = content + '\nexport PATH="$HOME/ai/bin:$PATH"  # ai-portal\n'
        act("update ~/.bashrc PATH (add ~/ai/bin)", lambda: bashrc.write_text(new), dry)
    else:
        ap.log("bashrc PATH already set (or no bashrc)")


def do_comfy_merge_done(dry: bool) -> None:
    for subdir, name in [("diffusion_models", "qwen_image_2512_fp8_e4m3fn.safetensors"),
                         ("loras", "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors")]:
        merged = ap.COMFY_MODELS / subdir / name
        src = ap.LEGACY_WS_COMFY / "models" / subdir / name
        if not merged.exists():
            ap.die(f"merged file missing — do NOT delete the workspace clone: {merged}")
        if sha256(merged) != sha256(src):
            ap.die(f"hash mismatch between {merged} and {src} — aborting")
        ap.log(f"verified merged: {subdir}/{name}")
    if ap.LEGACY_WS_COMFY.is_dir():
        act(f"remove stale workspace clone {ap.LEGACY_WS_COMFY} (~30 G)",
            lambda: shutil.rmtree(ap.LEGACY_WS_COMFY, ignore_errors=True), dry)


def do_remove_lmstudio(dry: bool) -> None:
    # every GGUF under ~/models must be byte-identical to one in the canonical store
    canon: dict[int, set[str]] = {}
    for p in ap.LLAMA_MODELS.rglob("*.gguf"):
        canon.setdefault(p.stat().st_size, set()).add(sha256(p))
    bad, total = [], 0
    for src in sorted(ap.LMSTUDIO_MODELS.rglob("*.gguf")):
        total += 1
        size = src.stat().st_size
        if size not in canon:
            bad.append((src, "size not found in canonical store"))
        elif sha256(src) not in canon[size]:
            bad.append((src, "hash mismatch"))
    ap.log(f"hash-verified {total} LM Studio GGUFs against canonical store")
    if bad:
        for src, why in bad:
            ap.warn(f"  NOT covered: {src} ({why})")
        ap.die("aborting: some LM Studio models are not in the canonical store")
    if not dry:
        aside = ap.ROLLBACK_DIR / "lmstudio-models"
        ap.ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        ap.log(f"moving {ap.LMSTUDIO_MODELS} -> {aside} (recoverable)")
        ap.LMSTUDIO_MODELS.rename(aside)
        ap.log(f"when you are confident, free the ~264 G with: rm -rf {aside}")
    else:
        ap.log("dry run: ~/models would be moved to state/rollback/lmstudio-models")


def do_archive_scripts(dry: bool) -> None:
    dest = ap.HOME / "ai-projects" / "ai-portal" / "archive" / "legacy"
    act(f"create {dest}", lambda: dest.mkdir(parents=True, exist_ok=True), dry)
    for name in HOME_SCRIPTS_AI:
        src = ap.HOME / name
        if src.exists():
            act(f"archive ~/{name}",
                lambda s=src, d=dest: shutil.move(str(s), str(d / s.name)), dry)
    for name in DOWNLOADS_JUNK:
        src = ap.HOME / "Downloads" / name
        if src.exists():
            act(f"archive ~/Downloads/{name}",
                lambda s=src, d=dest: shutil.move(str(s), str(d / s.name)), dry)


def do_remove_deb(dry: bool) -> None:
    deb = ap.HOME / "docker-desktop-amd64.deb"
    if deb.exists():
        act(f"remove {deb} (433 M)", lambda: deb.unlink(), dry)


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--after-cutover", action="store_true")
    ap_.add_argument("--comfy-merge-done", action="store_true")
    ap_.add_argument("--remove-lmstudio", action="store_true")
    ap_.add_argument("--archive-scripts", action="store_true")
    ap_.add_argument("--remove-deb", action="store_true")
    ap_.add_argument("--yes", action="store_true",
                     help="actually execute (default: dry-run)")
    args = ap_.parse_args()
    dry = not args.yes

    if not any([args.after_cutover, args.comfy_merge_done, args.remove_lmstudio,
                args.archive_scripts, args.remove_deb]):
        ap.die("nothing selected — pass --after-cutover / --comfy-merge-done / "
               "--remove-lmstudio / --archive-scripts / --remove-deb")

    if args.after_cutover:
        do_after_cutover(dry)
    if args.comfy_merge_done:
        do_comfy_merge_done(dry)
    if args.remove_lmstudio:
        do_remove_lmstudio(dry)
    if args.archive_scripts:
        do_archive_scripts(dry)
    if args.remove_deb:
        do_remove_deb(dry)
    ap.log("phase 5 complete")


if __name__ == "__main__":
    main()
