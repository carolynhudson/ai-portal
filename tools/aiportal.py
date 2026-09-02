#!/usr/bin/env python3
"""Shared configuration and helpers for ai-portal maintenance tools.

All paths for the consolidated AI runtime tree live here so that the whole
layout can be re-pointed by editing one file.

Conventions:
  - scripts are stdlib-only and idempotent
  - destructive operations are behind explicit flags and default to --dry-run
  - hard links (same filesystem) are used for model files: instant, zero space
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("AI_PORTAL_HOME", Path.home()))

# --- new canonical locations (portal-managed) -------------------------------
AI_ROOT = HOME / "ai"
LLAMA_REPO = AI_ROOT / "llama.cpp"                 # git clone, source only
BUILD_ROOT = AI_ROOT / "build"                     # disposable cmake build trees
RELEASES = AI_ROOT / "releases"                    # installed, self-contained releases
AI_BIN = AI_ROOT / "bin"                           # symlink farm -> current release
MODELS_ROOT = AI_ROOT / "models"
LLAMA_MODELS = MODELS_ROOT / "llama"               # router --models-dir
COMFY_REPO = AI_ROOT / "comfyui"                   # ComfyUI git clone
COMFY_VENV = COMFY_REPO / "venv"                   # ComfyUI virtualenv
COMFY_MODELS = COMFY_REPO / "models"
CONFIG = AI_ROOT / "config"
LLAMA_CONFIG = CONFIG / "llama"
PRESETS_INI = LLAMA_CONFIG / "presets.ini"
STATE = AI_ROOT / "state"
ROLLBACK_DIR = STATE / "rollback"
LOGS = AI_ROOT / "logs"
RELEASES_JSON = STATE / "llama-releases.json"

# --- legacy locations (current messy state) ----------------------------------
LEGACY_LLAMA_REPO = HOME / "llama.cpp"
LEGACY_LLAMA_BUILD = HOME / ".llama.cpp.build.106328"   # load-bearing (F3): keep until phase 3 deploy
LEGACY_LLAMA_SHARE = HOME / ".local" / "share" / "llama-cpp"  # stale clone
LEGACY_LLAMA_MODELS = HOME / ".cache" / "llama.cpp" / "models"
LEGACY_LOCAL_BIN = HOME / ".local" / "bin"
LEGACY_COMFY_REPO = HOME / "ComfyUI"
LEGACY_COMFY_VENV = HOME / "comfyui"
LEGACY_WS_COMFY = HOME / "workspace" / "ml_workspace" / "ComfyUI"
LMSTUDIO_MODELS = HOME / "models"
HF_CACHE = HOME / ".cache" / "huggingface"          # stays in place (F11)

# LunarG Vulkan SDK (dev-only: glslc, headers) — tarball installs, NOT apt.
# Lives under ~/ai/sdk/vulkan/<version>; newest version dir wins.
def _latest_vulkan_sdk() -> Path:
    root = HOME / "ai" / "sdk" / "vulkan"
    if root.is_dir():
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
        if dirs:
            return dirs[0]
    return root  # fallback; cmake args will simply fail loudly


VULKAN_SDK = _latest_vulkan_sdk()
HF_QWEN_BLOBS = HF_CACHE / "hub" / "models--unsloth--Qwen3.8-27B-GGUF" / "blobs"
HF_QWEN_MAIN = "af36ecb6b5db1407953345b746c14ac93f0657dda413910b4348683a2d990377"
HF_QWEN_MTP = "50d9ce5a6da381bbcfb31061cf73df94a90e6faf8efeddee379a9cb8f1501c6e"

# llama.cpp build settings (pinned from the known-good staging build CMakeCache)
LLAMA_CMAKE_ARGS = [
    "-DGGML_HIP=ON",
    "-DGGML_VULKAN=ON",
    "-DGGML_HIP_ROCWMMA_FATTN=ON",
    "-DAMDGPU_TARGETS=gfx1151",
    "-DCMAKE_BUILD_TYPE=Release",
    # self-contained release: binaries find their libs next to them
    "-DCMAKE_BUILD_RPATH=$ORIGIN",
    "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib",
    # Vulkan SDK lives under ~/vulkan with an arch subdir that CMake's find
    # modules won't probe on their own — point them at it directly
    f"-DCMAKE_PREFIX_PATH={VULKAN_SDK}/x86_64",
    f"-DVulkan_GLSLC_EXECUTABLE={VULKAN_SDK}/x86_64/bin/glslc",
    f"-DVulkan_GLSLANG_VALIDATOR_EXECUTABLE={VULKAN_SDK}/x86_64/bin/glslangValidator",
    f"-DVulkan_INCLUDE_DIR={VULKAN_SDK}/x86_64/include",
]

ROUTER_PORT = 8080
ROUTER_HOST = "127.0.0.1"


# ---------------------------------------------------------------- helpers ---
def log(msg: str) -> None:
    print(f"[ai-portal] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[ai-portal] WARNING: {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[ai-portal] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd: list[str], dry: bool = False, **kw) -> str:
    """Run a command, echo it, and return stdout (combined). Raises on failure."""
    cmd_s = " ".join(str(c) for c in cmd)
    log(f"$ {cmd_s}")
    if dry:
        return ""
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        die(f"command failed ({p.returncode}): {cmd_s}")
    return (p.stdout or "") + (p.stderr or "")


def link_file(src: Path, dst: Path, dry: bool = False) -> str:
    """Create dst as a hard link to src's contents (src may itself be a symlink).

    Returns one of: 'linked' | 'exists' | 'skipped'
    """
    if dst.is_symlink() or dst.exists():
        # verify identity where possible
        try:
            if dst.stat().st_ino == src.resolve().stat().st_ino and dst.stat().st_size == src.resolve().stat().st_size:
                return "exists"
        except OSError:
            pass
        die(f"destination exists and is not identical: {dst}")
    real = src.resolve()
    if not real.is_file():
        die(f"source missing: {real} (for {dst})")
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(real, dst)
    return "linked"


def make_symlink(target: Path, link_path: Path, dry: bool = False) -> str:
    if dry:
        if link_path.is_symlink() and link_path.resolve() == target.resolve():
            return "exists"
        log(f"would symlink {link_path} -> {target}")
        return "dry"
    if link_path.is_symlink():
        if link_path.resolve() == target.resolve():
            return "exists"
        link_path.unlink()
    elif link_path.exists():
        die(f"{link_path} exists and is not a symlink; refusing to replace")
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target)
    return "linked"


def ensure_dir(p: Path, dry: bool = False) -> None:
    if not p.exists():
        if not dry:
            p.mkdir(parents=True, exist_ok=True)
        log(f"mkdir {p}")


def human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"
