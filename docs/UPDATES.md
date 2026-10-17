# Update & Health Strategy

Which pieces of the AI stack update themselves, and which need the portal
(or a human) to act. Determined from what is actually installed on this box
(2026-08-31).

## Update matrix

| Component | Where | How it's installed | Updates itself? | Who drives updates |
|---|---|---|---|---|
| Ubuntu base, Mesa Vulkan **driver** (radv/`mesa-amdgpu-vulkan-drivers`) | system | apt (Ubuntu + `repo.radeon.com/graphics`) | **yes** — normal apt stream | OS; portal only surfaces pending updates |
| **ROCm** runtime/SDK (`amdrocm*` 7.14~pre3, HIP compiler, rocBLAS, etc.) | `/opt/rocm` | apt from `https://repo.radeon.com/graphics/26.13/ubuntu` (codename *resolute*) | **yes** — same apt repo; the "nightly steps" from AMD's dev site were a one-time repo bootstrap (the `amdgpu-install` deb in `~/Downloads`) | OS/apt. Portal should **watch**, not auto-apply (see coupling below) |
| **amdgpu kernel driver** | kernel module | `amdgpu-dkms` (DKMS) via apt | **yes**, but kernel updates need a **reboot** and trigger a DKMS rebuild | OS; portal surfaces "reboot pending" |
| **LunarG Vulkan SDK** (dev headers, glslc — build-time only) | `~/ai/sdk/vulkan/<ver>` (was `~/vulkan/`) | manual tarball | **no** | **Portal or user.** Tarball URL is predictable: `https://sdk.lunarg.com/sdk/download/$VERSION/linux/vulkansdk-linux-x86_64-$VERSION.tar.xz`; current version discoverable from `https://vulkan.lunarg.com/sdk/home` (scrape or manual entry) |
| **llama.cpp** | `~/ai/llama.cpp` (source), `~/ai/releases/llama.cpp-b<ver>` (deploy) | git + local CMake build | **no** | **Portal** — `tools/phase3_rebuild_llama_cpp.py` flow: fetch pinned commit → build in `~/ai/build/` → stage self-contained release → swap `~/ai/bin/` symlinks → restart `ai-llama-router` |
| **ComfyUI** | `~/ai/comfyui` (source) + `venv/` | git + venv pip | **no** | **Portal** — `git fetch/checkout <tag>` + `pip install -r requirements.txt` (torch wheel is pinned, so no 5 GB re-download on normal updates) |
| **huggingface** tools | system pip + `~/ai-projects/gguf-indexer/venv` | pip/uv | **no** (minor) | Portal |
| **NPU stack** (XRT userspace `/opt/xilinx/xrt` + `xrt-amdxdna` DKMS) | `/opt/xilinx/xrt`, dkms | **self-built** debs from `amd/xdna-driver` source (`tools/npu_xrt_build.sh`), DKMS MOK-signed | **no** — no apt stream; upstream ships EULA-gated debs (1.8 pairs with XRT 2.25.37) | **User.** Rebuild via the script, or switch to AMD's EULA debs (see `~/ai/logs/NPU-morning-brief.md`) |
| **Ryzen AI SDK** (ORT VitisAI EP) | `~/ai/venvs/ryzenai` (py3.12) | SDK 1.8 tgz **manual** recipe (installer's ensurepip step fails on 26.04) + PyPI `onnxruntime-vitisai` | **no** | **User.** The manual wheel set is unvalidated against AMD's pins — top suspect in the current engine block |
| **Models** (GGUF, checkpoints, LoRAs) | `~/ai/models/llama`, `~/ai/comfyui/models`, `~/.cache/huggingface` | `hf` CLI / ComfyUI manager | **no** | **Portal** — see "Model downloads" below |

## Version-coupling watch items (the important part)

The GPU stack has three interlocking versions the portal health pane must
track together:

1. **ROCm installed** (`amdrocm` apt version — currently `7.14.0~pre3`)
2. **ROCm version llama.cpp was compiled against** (build log: hipcc from
   `/opt/rocm/core-7.14`; record at build time in
   `~/ai/state/llama-releases.json`)
3. **ROCm version the ComfyUI torch wheel was built for**
   (`torch 2.12.0a0+rocm7.13.0a20260411` — note: **7.13**, one behind the
   system; torch bundles its own ROCm libs so both coexist in `/opt/rocm/core*`)

Rule for the portal: after any apt run, compare (1) against (2) and (3).
If the major.minor moved, offer **rebuild llama.cpp** (cheap, ~10 min) and
**reinstall ComfyUI torch** (larger). Until then, the running binaries keep
working (ABI is stable within a series), so this is advisory, not urgent.

Similarly, the **Vulkan SDK version used at build time** is recorded per
release; a newer SDK in `~/ai/sdk/vulkan/` doesn't require a rebuild, but
the health pane can offer one (new glslc can improve Vulkan shader codegen).

The NPU has the same three-way coupling, learned the hard way (Oct 17):
**XRT userspace version ↔ DKMS kernel driver ↔ the Ryzen AI SDK's validated
partner**. The SDK validates against exactly one XRT release per version
(1.8 → 2.25.37); our self-built 2.27 SHIM runs the driver and `xrt-smi
validate` fine, but the SDK's compiled-model flow (VAIML DD-xclbin runner,
`com.ryzenai` custom-op registration) sits on that drift and is currently
blocked (forensics: `~/ai/logs/NPU-morning-brief.md`, memory ids 13–14).
Rule: when rebuilding XRT, pin to the SDK's stated partner, not to HEAD;
and the DKMS driver version must match the SHIM (same build).

## What the portal health pane should surface

- apt: pending updates touching `amdrocm*|amdgpu*|mesa-amdgpu*` + kernel
  (reboot-pending flag)
- ROCm version vs llama.cpp build version vs ComfyUI torch ROCm (mismatch ⇒
  rebuild/reinstall offer)
- NPU (XDNA) tile, added Oct 17: `/dev/accel/accel0` presence, `amdxdna`
  module version, firmware, `xrt-smi examine` device list, memlock, and the
  embedding endpoints on 8390 (CPU sidecar) / 8391 (NPU shim) — the four
  layers (driver → node → XRT → engine) each reported separately
- Vulkan SDK installed versions under `~/ai/sdk/vulkan/` + latest known
- llama.cpp: local commit vs `origin` (git ls-remote), deployed release,
  router uptime (`systemctl --user status ai-llama-router`)
- ComfyUI: local commit vs origin, venv python/torch versions
- Disk: `~/ai` tree sizes, HF cache size, rollback dir size
- Model store: INDEX.json freshness (gguf-indexer), orphaned/unknown files
  in the router tree, broken hardlinks

## Model downloads (the HF-command idea)

The user copies a command like
`hf download unsloth/Qwen3.8-27B-GGUF --include "*.gguf"` from Hugging Face
and hands it to the portal; the portal parses it and runs the download through
its own `huggingface_hub` code so it can:

- decide placement by file role:
  - `*.gguf` (no `mmproj`/draft prefix) → `~/ai/models/llama/<ModelName>/`
  - `mmproj*` → same dir as its model
  - `mtp-*`/`dspark-*`/`dflash-*` → same dir as its model (see note below)
- skip files that exist with identical size+hash (incremental)
- create the per-model subdirectory the router needs (it does **not** mirror
  HF's `snapshots/<commit>/MTP/` layout — the router doesn't understand it)
- re-run the gguf-indexer and refresh `presets.ini`
- restart (or hot-reload via API) the router so the new model appears

## Model layout rules (code-verified, llama.cpp b10641)

From `common/preset.cpp::load_from_models_dir` + `fs_list`:

- `--models-dir` is scanned **one level deep**.
- Each **immediate subdirectory** is a model; the directory name is the
  public model id.
- Inside a model dir, only **immediate files** are inspected — **sub-sub-
  folders are NOT scanned**. So:
  - `mmproj*` files: flat in the model dir, auto-attached.
  - draft/MTP files: must be flat in the model dir and **named with a
    matching prefix**: `mtp-`, `dspark-`, or `dflash-` (first match wins).
    A `draft/` or `mtp/` subfolder would be ignored.
- Loose `.gguf` files directly in `--models-dir` become single-file models
  (no sidecars); loose `mmproj*`/draft files are ignored entirely.
- Split models: the file containing `-00001-of-` is used.

**Toggling a draft on/off for benchmarking:** the router auto-attaches any
matching file, so the simplest switch is to rename it out of the prefix,
e.g. `mtp-Qwen3.8-27B-Q4_0.gguf` → `Qwen3.8-27B-Q4_0-mtp.gguf` (no prefix ⇒
ignored), and rename back to re-enable. (A preset can also pin an explicit
`model-draft = <path>`; the portal can use that for A/B setups.)

**mmproj placement (your LM-Studio-style question):** yes, keeping the
`mmproj-*.gguf` in the same folder as its parent `.gguf` is exactly what the
router expects — verified live (all four vision models report their
`mmproj = …` line in the router's preset view).

## Safety note: old `~/.local/bin` binaries

The legacy binaries in `~/.local/bin` have RUNPATH baked to
`~/.llama.cpp.build.106328/build/bin` — do **not** delete the staging trees
until the Phase 4 cutover has happened **and** the router (plus any workers
it spawns) runs from `~/ai/bin`. The new release is verified self-contained
(`ldd` clean + full worker-load smoke test), so after cutover the staging
trees and `~/.local/share/llama-cpp` are safe to remove (Phase 5
`--after-cutover`).

## Decisions recorded here

- `qwen-image-2512` (21 G) — **excluded** from the canonical store at the
  user's request (LM Studio image-model experiment; ComfyUI covers the use
  case). It remains in `~/models/` until Phase 5 removes that tree.
- Vulkan SDK home: `~/ai/sdk/vulkan/<version>` (compat symlink
  `~/vulkan` → new location). Build scripts auto-select the newest version
  directory (`tools/aiportal.py::_latest_vulkan_sdk`).
