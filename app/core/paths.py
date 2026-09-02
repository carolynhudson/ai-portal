"""Canonical paths for the AI runtime tree (~/ai) — single source of truth.

Ported from ``tools/aiportal.py`` so the portal and the legacy phase-scripts
share one layout. Everything derives from :data:`AI_ROOT` (or ``HOME``); no
other file hard-codes an absolute path. Re-point the whole tree by setting
``AI_PORTAL_HOME`` (rarely needed).
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("AI_PORTAL_HOME", str(Path.home())))

AI_ROOT = HOME / "ai"

# --- llama.cpp -------------------------------------------------------------
LLAMA_REPO = AI_ROOT / "llama.cpp"      # source clone (git)
BUILD_ROOT = AI_ROOT / "build"          # disposable cmake build trees
RELEASES = AI_ROOT / "releases"         # installed, self-contained releases
AI_BIN = AI_ROOT / "bin"                # symlink farm -> current release
MODELS_ROOT = AI_ROOT / "models"
LLAMA_MODELS = MODELS_ROOT / "llama"    # router --models-dir (one subdir per model)

# --- ComfyUI ----------------------------------------------------------------
COMFY_REPO = AI_ROOT / "comfyui"        # git clone (repo + venv + models together)
COMFY_VENV = COMFY_REPO / "venv"
COMFY_MODELS = COMFY_REPO / "models"

# --- Config -----------------------------------------------------------------
CONFIG = AI_ROOT / "config"
LLAMA_CONFIG = CONFIG / "llama"
PRESETS_INI = LLAMA_CONFIG / "presets.ini"

# --- State (runtime data; outside the repo) ---------------------------------
STATE = AI_ROOT / "state"
RELEASES_JSON = STATE / "llama-releases.json"   # machine-readable release registry
ROLLBACK_DIR = STATE / "rollback"
LOGS = AI_ROOT / "logs"
PORTAL_STATE = STATE / "portal"                 # portal.db, secrets, job logs

# --- Hugging Face (kept in place per F11 — do NOT move) ---------------------
HF_CACHE = HOME / ".cache" / "huggingface"

# --- systemd *user* unit names (owned by the portal) ------------------------
ROUTER_UNIT = "ai-llama-router"
COMFY_UNIT = "ai-comfyui"
PORTAL_UNIT = "ai-portal"

# --- Router network defaults (the router this session may ride on) ----------
ROUTER_HOST = "127.0.0.1"
ROUTER_PORT = 8080


def _latest_vulkan_sdk() -> Path:
    """Newest ``~/ai/sdk/vulkan/<version>`` dir. Falls back to ``~/vulkan``."""
    root = AI_ROOT / "sdk" / "vulkan"
    if root.is_dir():
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
        if dirs:
            return dirs[0]
    return HOME / "vulkan"


VULKAN_SDK = _latest_vulkan_sdk()

# llama.cpp build settings (pinned from the known-good staging build). Used by the
# Phase C update pipeline; auto-selects the newest Vulkan SDK at build time.
LLAMA_CMAKE_ARGS = [
    "-DGGML_HIP=ON",
    "-DGGML_VULKAN=ON",
    "-DGGML_HIP_ROCWMMA_FATTN=ON",
    "-DAMDGPU_TARGETS=gfx1151",
    "-DCMAKE_BUILD_TYPE=Release",
    # self-contained release: binaries find their libs next to them
    "-DCMAKE_BUILD_RPATH=$ORIGIN",
    "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib",
    f"-DCMAKE_PREFIX_PATH={VULKAN_SDK}/x86_64",
    f"-DVulkan_GLSLC_EXECUTABLE={VULKAN_SDK}/x86_64/bin/glslc",
    f"-DVulkan_GLSLANG_VALIDATOR_EXECUTABLE={VULKAN_SDK}/x86_64/bin/glslangValidator",
    f"-DVulkan_INCLUDE_DIR={VULKAN_SDK}/x86_64/include",
]
