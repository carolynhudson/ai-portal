# Cleanup & Foundation Plan

Goal: turn the current scattered, half-broken AI tool environment into a single
portal-managed tree (`~/ai/`) with correct llama.cpp router layout, safe update/
deploy mechanics, and a clean foundation the web portal can build on.

Companion to `docs/ENVIRONMENT.md` (findings F1–F11 referenced below) and
`docs/PORTAL_PLAN.md` (the portal design this cleanup enables).

**Status: all phases complete (2026-08-31).**

---

## 1. Target layout

```
~/ai/                                  # single portal-managed runtime root
├── llama.cpp/                         # llama.cpp git repo (source only, stays clean)
├── build/                             # disposable build trees (rebuildable any time)
│   └── llama.cpp-b10641/              #   one dir per build
├── releases/
│   └── llama.cpp-b10641/              # installed release: bin/ lib/ include/
│                                      #   self-contained (RPATH=$ORIGIN/../lib)
├── bin/                               # current-version symlinks -> releases/llama.cpp-<ver>/bin/*
│   ├── llama-server                   #   PATH entry for this dir replaces ~/.local/bin
│   └── ...
├── models/
│   └── llama/                         # router --models-dir  (ONE subdir per model)
│       ├── Qwen3.8-27B-UD-Q8_K_XL/
│       │   ├── Qwen3.8-27B-UD-Q8_K_XL.gguf
│       │   └── mtp-Qwen3.8-27B-Q4_0.gguf          <- auto-attached draft (fixes F1)
│       ├── Laguna-S-2.1-Q4_K_M/                   <- BOTH shards, one model (fixes F2)
│       │   ├── …-00001-of-00002.gguf
│       │   └── …-00002-of-00002.gguf
│       └── <11 more model dirs, each with its mmproj-*.gguf inside>
├── comfyui/                           # ComfyUI git repo (was ~/ComfyUI)
│   ├── venv/                          # venv (was ~/comfyui) — fixed F8
│   ├── models/                        # ComfyUI category dirs (92 G + merged 21.6 G)
│   └── user/                          # workflows, comfyui.db (stays with repo)
├── config/
│   └── llama/presets.ini              # validated per-model tuning (fixes F4)
├── state/                             # portal state: releases.json, rollback snapshots,
│   └── rollback/                      #   old flat model dir (until Phase 5)
└── logs/

~/.cache/huggingface/                  # KEPT in place (F11) — hf CLI / transformers /
                                       # llama.cpp all default here; portal manages via hf
~/.config/systemd/user/ai-llama-router.service   # router as a user service (fixes F9)
```

**Why `~/ai/` and not inside the ai-portal repo:** the repo will hold portal *code*;
putting 400 GB of weights + build trees inside a git project breaks `git status`,
IDE indexing and makes the repo unshippable. `~/ai/` is the portal's runtime root,
referenced by one constant; a symlink `ai-portal/runtime → ~/ai` can be added later if
the "under the portal folder" view is desired. ComfyUI/llama.cpp both accept arbitrary
paths (`--models-dir`, `extra_model_paths.yaml`, env vars), so nothing is forced on us.

**Why hard links, not copies:** everything is on the same NVMe. Reorganizing 300 GB
of weights with `os.link()` is instant, uses 0 extra space, and deleting either link
later is safe (data lives until the last link goes).

## 2. Phases

Owner: **me** = agent (safe, reversible, verified) · **user** = you, from another
terminal (because it stops the router that is serving this very session).

| Phase | What | Owner | Risk / rollback |
|---|---|---|---|
| 1 | Build `~/ai/models/llama/` (hardlinks) + validated `presets.ini` | me (done this session) | 0 — nothing existing is modified |
| 2 | Move `~/llama.cpp`, `~/ComfyUI`, `~/comfyui` venv into `~/ai/`; merge the 2 unique workspace models; leave compat symlinks at old paths | me — **done** | low — plain `rename(2)` on same FS, symlinks keep old paths working |
| 3 | Rebuild llama.cpp at pinned commit with self-contained RPATH → `~/ai/releases/llama.cpp-b10641/`, symlink farm `~/ai/bin/` | me — **done** | 0 for running system; old deploy untouched |
| 4 | **Cutover**: stop old router, move old flat model dir to `state/rollback/`, install `ai-llama-router.service`, start, verify model list | user — **done** | ~2 min downtime; full rollback file written by the script (restart old router with old paths) |
| 5 | Cleanup: delete old flat dir, staging dir (F3), stale clone (F7), `~/.local/bin` llama junk, workspace ComfyUI dup (F8), optional `~/models` deletion (F6, 264 G), archive home scripts | user (script, explicit flags) — **done** | every deletion behind a flag + dry-run first; `~/models` deletion additionally hash-verifies the canonical store first |

Remaining: `~/ai/state/rollback/lmstudio-models` (244 G, hash-verified
originals) awaits user confirmation to delete; see PORTAL_PLAN.md D5.

### Phase 4 detail (what changes for you)
- Router command becomes:
  `~/ai/bin/llama-server --host 127.0.0.1 --port 8080 --models-max 1 \
     --models-dir ~/ai/models/llama --models-preset ~/ai/config/llama/presets.ini`
- Managed as systemd user service `ai-llama-router` (restart-on-failure, journal logs).
- Port 8080, localhost-only — unchanged.
- Verification asserts: all 13 model dirs present, no `-00001-of-`/`-00002-of-` ids,
  Laguna is a single entry, Qwen3.8 preset contains `model-draft` (MTP),
  gemma/ThinkingCap/Qwen3.6-35B/muse-glimmer presets contain their `mmproj`.
  (The HF-cache entry `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL` remains as a duplicate
  alias for the same file — cosmetic; the portal can prune it later.)
- **Expect this session to drop when the router restarts — that's the point.**

### Update policy (what the bash script was trying to do, done properly)
- llama.cpp: `git fetch` → build from **pinned commit/tag** in `~/ai/build/llama.cpp-<id>`
  → `cmake --install` to `~/ai/releases/llama.cpp-<id>` (self-contained RPATH) →
  verify (`--version`, `ldd` clean, optional smoke run) → swap `~/ai/bin` symlinks →
  `systemctl --user reload-or-restart` the router. Previous release kept for rollback.
  The repo stays source-only (no build dirs inside the clone — the original mess).
- ComfyUI: `git fetch` + `git checkout <tag>` + venv `pip install -r requirements.txt`
  (torch pinned; never re-downloaded unless required) → service restart.
- HF: `hf` CLI for downloads; cache pruned with `hf`/`huggingface_hub` APIs.

## 3. Verification performed this session

- Router layout rules read from llama.cpp source (not just README):
  `common/preset.cpp::load_from_models_dir`, `common/common.cpp::fs_list`,
  `tools/server/server-models.cpp`, `common/arg.cpp` (arg names for presets).
- Live `/v1/models` inspected to document the broken state (F1/F2/F5).
- `readelf -d` on deployed binaries proved the staging-dir RUNPATH dependency (F3).
- All 19 LM Studio GGUFs size-compared 1:1 against the flat copy (only
  `qwen-image-2512-Q8_0.gguf` missing from the flat copy).
- Phase 3 build + **two live test routers on port 18080** (old binary, then the
  new `b10641` release binary) validate the new tree + presets: 14 model entries
  (13 local + 1 HF-cache duplicate),
  no shard-only ids, `model-draft` attached to Qwen3.8, `mmproj` attached to all
  four vision models, all tuning keys applied. `ldd` on the release binary is
  clean (no staging-dir references). Production on :8080 verified untouched.

## 4. Open decisions (answer when convenient; defaults shown)

1. Delete `~/models` (264 G) after Phase 5 hash-verify? **default: yes, via `--remove-lmstudio`.**
2. Delete the 433 M `docker-desktop-amd64.deb` and `~/Downloads` junk? **default: yes.**
3. Router host: keep `127.0.0.1` or open `0.0.0.0` (LAN access)? **default: 127.0.0.1.**
4. llama.cpp update pinning: track `master` (current) or latest release tag?
   **default: pin to the commit you approve; the script supports both.**
5. Keep the boot/UEFI scripts in `~/` (rEFInd etc.)? **default: leave untouched.**

## 4a. NPU (XDNA) sidecar compute — PINNED WATCH, ongoing (added Oct 17 2026)

**Grep this section for: CastAvx, m_length, embeddinggemma, "model generation", relnotes.**
This is a standing task: every future session that touches the NPU, AMD drivers,
or sidecar services re-runs the watch list (§4d) before starting new work.

### Why we want the NPU at all
The router runs `--models-max 1` on purpose (owner's OOM history: a model in the
router is an agent's eviction). The services we want to grow are small, streaming,
and latency-tolerant — exactly the shape the NPU is for:

- **RAG embeddings** (live on the CPU sidecar today: EmbeddingGemma-2, 111 docs/s,
  durable `ai-embed` unit). The endpoint is plain `/v1/embeddings`; the NPU is a
  drop-in engine behind it, not a redesign.
- **Autocomplete / fill-in-the-middle** small models, **speech-to-text decode**
  (whisper is a first-class Ryzen AI target), rerankers.

The NPU's real claim on this box is **zero contention**: GPU and CPU stay with
the router and the agents while embeddings/ASR run on idle silicon. Adoption is
**benchmark-gated**: NPU+GPU vs GPU-only on the same benchmark, measured, before
committing (owner rule). The CPU arm (111 docs/s) is the number to beat;
`tools/npu_parity.py` runs the arms.

### State: proven (Oct 17, survives the ROCm 31.60 update + reboot)
Kernel/driver: DKMS `xrt-amdxdna 2.27.0` MOK-signed, fw `npu.dev.sbin`,
node `/dev/accel/accel0` (open() proven), memlock unlimited, power=performance.
XRT userspace (self-built 2.27): `xrt-smi examine` sees
`[0000:c4:00.1] NPU Strix Halo aie2p 6x8`; `xrt-smi validate` passes **with
visible NPU utilization**. SDK 1.8 compiles FOR the NPU: quicktest compiles the
CNN for `AMD_AIE2P_4x8_CMC_Overlay` (398 ops on NPU, `Compile done`).
Health page shows the four-layer ladder (driver→node→XRT→engine) live.

### State: blocked (SDK 1.8 Python userspace ONLY — kernel/XRT exonerated)
1. **quicktest JIT**: compiles, then aborts
   `runner_requests_queue.cpp:178 Failed to create runner: xclbin: m_length does
   not match buffer size`. Identical on in-tree AND DKMS 2.27 drivers → not the kernel.
2. **AMD precompiled EG-300M** (`amd/embeddinggemma-300m_npu_rai_1.8.0_npu_4K`,
   in `~/ai/models/npu/embeddinggemma-300m-rai18`): graph load fails
   `com.ryzenai:CastAvx(-1) is not a registered function/op`.
   `register_custom_ops_library(voe/lib/libonnxruntime_vitis_ai_custom_ops.so)`
   (exports RegisterCustomOps) does not fix; `import ryzenai_dynamic_dispatch`
   does not fix; **CPUExecutionProvider reproduces it identically** → the schema
   wall lives entirely in the SDK Python userspace.

Primary suspect: the SDK install is the **manual** recipe (the installer's
ensurepip step fails on 26.04); manual skips AMD's pinned wheel set. The venv
has PyPI `onnxruntime-vitisai 1.27.0` + `voe 1.8.0` — version-plausible,
pin-unvalidated. AMD's validated pairing is SDK 1.8 ↔ **XRT 2.25.37** (the
EULA-gated debs); we run self-built 2.27. AMD 1.8 release notes confirm
embeddinggemma-300m as the supported 1.8 embedding model and state
"model generation is not supported on Linux in this release" (LLM-class — the
JIT path failing is consistent with a Linux gap).

### Paths to a working NPU (none of them is "wait for AMD")
- **A. Validated stack** *(recommended, owner-only step)*: download the four
  EULA-gated 1.8 debs (browser EULA: `xrt_202620.2.25.37_24.04`
  base/base-dev/npu + `xrt_plugin.2.25…amdxdna`), `sudo apt install`, then
  quicktest → expect `Test Finished`, then the 300M probe. This replaces the
  self-built 2.27 pairing with AMD's validated one. It is the ONLY path we have
  never tested, and the only one with AMD's stamp.
- **B. Fix the installer route**: re-run AMD's installer on 26.04 and patch its
  ensurepip failure by hand (python3.12 venv + AMD's pinned wheels). Separates
  "wheel pins" from "XRT 2.25" as causes — test A and B independently.
- **C. FastFlowLM / Lemonade** (pip/appimage, self-contained XRT, Strix Halo
  supported, NPU embedding models exist): Ubuntu channel is a PPA whose deps
  apt-pull XRT 2.25 → owner-policy call (AMD components via AMD channels only).
- **D. Upstream the repro**: CastAvx registration + `m_length` are minimal,
  public-file repros for the RyzenAI-SW issues tracker.
- **E. Stay CPU-only**: the `ai-embed` sidecar unit is durable and EG2 on CPU is
  genuinely good; NPU remains validated, monitored, unadopted. Zero risk.
- Naming guard: the AMD "EmbeddingGemma-300M" compiled repo is the FIRST-GEN
  model (768d, same prefixes). Our RAG baseline is **EmbeddingGemma-2** (EG2,
  78.68 MTEB-code). 300M-on-NPU is a *speed* reference arm; never let a speed
  test smuggle in a quality change.

### The watch list (per session — what to look for and why)
1. `https://ryzenai.docs.amd.com/en/latest/relnotes.html` — new SDK (1.9+)?
   Look for: "model generation supported on Linux", the **XRT version pairing**
   named for the new SDK (A/B choice depends on it), new embedding models
   (EG2?), and known-issues entries matching `CastAvx` / `m_length` /
   registration. Why: A vs B is decided by the pairing line.
2. `github.com/amd/RyzenAI-SW` — does the embeddinggemma **Linux example**
   return (the Oct-2026 layout has no `example/` tree and no embeddinggemma
   example on main/v1.8.0)? Its provider-options block is the canonical
   CastAvx-registration answer (custom-op library + pins).
3. `onnxruntime-vitisai` PyPI releases — AMD's own pin notes; if a release
   declares its voe/XRT pairing, B becomes mechanical.
4. ROCm hip headers: when `amdrocm-core-dev` > 10.1.0-3 ships declaring the
   four scalar f32 ocml prototypes (`__ocml_{exp10,log,log10,log2}_f32`),
   **delete `~/ai/config/shims/hip_bf16_log2.h`** (hip builds go plain; the
   health page's HIP probe turns green on its own).
5. AMD XRT releases (github.com/Xilinx/XRT, amd/xdna-driver): `m_length` /
   runner-queue fixes relevant to VAIML DD-xclbins.

### Re-test ladder (run when any watch item fires)
`tools/npu_validate.sh` (layers) → `quicktest.py` (expect `Test Finished`) →
300M probe (`/tmp/probe300.py` pattern: custom-ops + meta_format bin) →
`tools/npu_embed_server.py --backend amd-onnx` on :8391 →
`tools/npu_parity.py` arms vs **111 docs/s** → agent-under-load arms
(closed-session) → record in memory + update this section.

Artifacts: full forensics `~/ai/logs/NPU-morning-brief.md`; memory ids
8, 10–14 (trail), 16–17 (ROCm 10.1 hip gap + shim), 18 (b11715).
Tools in repo: `tools/npu_validate.sh`, `tools/npu_parity.py`,
`tools/npu_embed_server.py` (engine-agnostic shim; amd-onnx backend written,
awaiting engine), `tools/npu_xrt_build.sh`, `tools/embed_sidecar.sh` (+ `ai-embed` unit).

## 5. What this means for the portal (inputs for the next design step)

- **Tool providers**: `llamacpp`, `comfyui`, `huggingface` — each exposes
  `status/version/update/deploy/config` to the portal backend.
- **Model service**: scan `~/ai/models/llama` (subdir = model, sidecar pairing rules
  from ENVIRONMENT §4), GGUF metadata via the `gguf` python lib (already in the
  gguf-indexer venv — consider merging that project's code into the portal),
  generate/maintain `presets.ini`, download via `hf`, dedup by content hash.
- **Service manager**: systemd user units for router (+ ComfyUI later); the portal
  start/stop/tail-logs them.
- **Update pipeline**: the Phase 3/4 mechanics *are* the portal's llama.cpp updater —
  the temporary scripts are its reference implementation.
- **State**: SQLite in `~/ai/state/` (tool registry, releases, update history,
  download jobs, model inventory).
- Backend: Python 3.14 + FastAPI + uv-managed venv (suggested; decided in portal design).
