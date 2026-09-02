#!/usr/bin/env python3
"""Phase 2 — consolidate repos & venv into ~/ai (non-destructive).

Moves (same-filesystem rename, instant):
  ~/llama.cpp   -> ~/ai/llama.cpp
  ~/ComfyUI     -> ~/ai/comfyui
  ~/comfyui     -> ~/ai/comfyui/venv      (+ shebang fixes for the venv)
Merges the 2 unique model files from the stale workspace ComfyUI clone via
hard links (duplicates are skipped).
Leaves compatibility symlinks at the old paths so anything still referencing
them keeps working.

Usage:
  python3 phase2_move_repos.py --dry-run
  python3 phase2_move_repos.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiportal as ap

# unique files in the stale workspace ComfyUI clone that must be preserved
WS_UNIQUE = [
    ("diffusion_models", "qwen_image_2512_fp8_e4m3fn.safetensors"),
    ("loras", "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors"),
]


def move_dir(src: Path, dst: Path, dry: bool) -> None:
    if not src.exists():
        ap.warn(f"{src} not found — skipping (already moved?)")
        return
    if dst.exists():
        ap.die(f"destination {dst} already exists")
    ap.log(f"move {src} -> {dst}")
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    ap.make_symlink(dst, src, dry)


def fix_venv_shebangs(venv: Path, old_prefix: str, new_prefix: str, dry: bool) -> None:
    fixed = 0
    if not venv.exists():
        return
    for p in list((venv / "bin").glob("*")):
        if p.is_dir() or p.is_symlink():
            continue
        try:
            first = p.open("rb").read(256)
        except OSError:
            continue
        if not first.startswith(b"#!"):
            continue
        line = first.split(b"\n", 1)[0]
        if old_prefix.encode() in line:
            ap.log(f"  shebang fix: bin/{p.name}")
            if not dry:
                data = p.read_bytes()
                new = data.replace(old_prefix.encode(), new_prefix.encode(), 1)
                p.write_bytes(new)
            fixed += 1
    ap.log(f"venv shebangs fixed: {fixed}")


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--dry-run", action="store_true")
    args = ap_.parse_args()
    dry = args.dry_run

    ap.ensure_dir(ap.AI_ROOT, dry)

    # safety: no process may be *running from* a path we are about to move
    # (matching cmdline strings is not enough: the router's --models-dir
    # contains ".cache/llama.cpp" but its binary lives in ~/.local/bin)
    moving = {str(ap.LEGACY_LLAMA_REPO), str(ap.LEGACY_COMFY_REPO), str(ap.LEGACY_COMFY_VENV)}
    import os
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        for m in moving:
            if exe == m or exe.startswith(m + "/"):
                ap.die(f"process {pid} is running from {m}: {cmd.strip()}")

    move_dir(ap.LEGACY_LLAMA_REPO, ap.LLAMA_REPO, dry)
    move_dir(ap.LEGACY_COMFY_REPO, ap.COMFY_REPO, dry)

    # venv: ~/comfyui -> ~/ai/comfyui/venv
    if ap.LEGACY_COMFY_VENV.exists() and not ap.COMFY_VENV.exists():
        ap.log(f"move venv {ap.LEGACY_COMFY_VENV} -> {ap.COMFY_VENV}")
        if not dry:
            ap.LEGACY_COMFY_VENV.rename(ap.COMFY_VENV)
        ap.make_symlink(ap.COMFY_VENV, ap.LEGACY_COMFY_VENV, dry)
        fix_venv_shebangs(ap.COMFY_VENV, str(ap.LEGACY_COMFY_VENV), str(ap.COMFY_VENV), dry)

    # merge unique models from the stale workspace clone
    for subdir, name in WS_UNIQUE:
        src = ap.LEGACY_WS_COMFY / "models" / subdir / name
        dst = ap.COMFY_MODELS / subdir / name
        if not src.exists():
            ap.warn(f"workspace model missing: {src}")
            continue
        st = ap.link_file(src, dst, dry)
        ap.log(f"  [{st:8s}] comfyui/models/{subdir}/{name} ({ap.human(src.stat().st_size)})")

    # verifications (skipped in dry-run)
    if not dry:
        ap.log("verifying moved repos...")
        for repo in (ap.LLAMA_REPO, ap.COMFY_REPO):
            st = ap.run(["git", "-C", str(repo), "status", "-sb"]).splitlines()[0]
            ap.log(f"  git {repo.name}: {st}")
        out = ap.run([str(ap.COMFY_VENV / "bin" / "python"), "-c",
                      "import torch,sys;print('torch',torch.__version__)"])
        ap.log(f"  venv check: {out.strip()}")
    ap.log("phase 2 complete")


if __name__ == "__main__":
    main()
