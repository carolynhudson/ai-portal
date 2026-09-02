# Environment Report — AI Tool Management Portal (pre-build audit)

Audited: 2026-08-30, on the live system, without touching the second NVMe drive.
Method: filesystem/process inspection + reading the **source code** of the deployed
llama.cpp (not just its docs) to confirm exact router behavior.

---

## 1. Hardware & OS

| Item | Value |
|---|---|
| Machine | Framework Desktop, dual-boot |
| CPU/GPU | AMD Ryzen AI Max+ 395 (Strix Halo), iGPU RDNA 3.5 (`gfx1151`), 128 GB shared RAM (122 GB visible) |
| This OS | **Ubuntu 26.04.1 LTS**, kernel `7.0.0-30-generic`, on `nvme0n1` only |
| Other OS | `nvme1n1` (2 TB, 7 partitions) — **not mounted, not scanned, not touched** (per requirement) |
| Toolchain | GCC 15.2, CMake 4.2.3, Ninja 1.13.2, Make 4.4.1, git 2.53, Python 3.14.4 (system) |
| GPU stack | ROCm SDK at `/opt/rocm` (7.x, `rocm-smi` works, GPU visible as Device 0, DID 0x1586); Vulkan 1.4.341 (system loader + SDK copy in `~/vulkan/1.4.341.0`); `~/.cache/comgr` = ROCm compiler cache |
| AI tools present | llama.cpp (built, running), ComfyUI (cloned, not running), huggingface-cli / `huggingface_hub` 1.2.2 (system pip) + 1.29.0 (gguf-indexer venv) |
| Disk | `nvme0n1p2` = `/`: 1.8 T, 757 G used, **982 G free** — plenty for any layout |

## 2. What is running right now

```
PID 694316  llama-server --models-dir /home/cortana/.cache/llama.cpp/models/ --models-max 1 --port 8080   (router)
PID 694601  /home/cortana/.local/bin/llama-server --host 127.0.0.1 --port 45097 \
            --alias Qwen3.8-27B-UD-Q8_K_XL --model .../Qwen3.8-27B-UD-Q8_K_XL.gguf   (worker, GPU 93% busy)
```

The router (port 8080) is what this agent session is served through — **any
cutover that stops it will end the current AI session. That step must be run
by the user from a separate terminal** (see `docs/PLAN.md`, Phase 4).

Deployed binary: `~/.local/bin/llama-server`, `version: 0.3.0-dev (build 10641, commit 539f24529)`,
built with HIP + Vulkan backends.

## 3. Inventory (sizes, locations)

| Path | Size | What it is |
|---|---|---|
| `~/models/` | **264 G** | Original LM Studio tree (org/model subfolders, 19 GGUFs + 1 `config.json`) |
| `~/.cache/llama.cpp/models/` | **244 G** | Flat "mass copy" of the LM Studio models + 2 HF symlinks + `presets.ini`, `INDEX.json`, stale `gguf-indexer.py` |
| `~/.cache/huggingface/` | 31 G | HF hub cache, one repo: `unsloth/Qwen3.8-27B-GGUF` (blob `af36…`=31.5 GB main, blob `50d9…`=1.4 GB MTP draft; snapshot has `MTP/` subfolder) |
| `~/ComfyUI/` | 91 G | ComfyUI git clone @ `77739723` (clean, up to date); `models/` has proper category layout (diffusion_models 60 G, text_encoders 23 G, checkpoints 6.9 G, loras 1.3 G, vae 562 M); `user/` has workflows + `comfyui.db`; custom_nodes effectively empty |
| `~/comfyui/` | 7.8 G | **The venv for ComfyUI** (lowercase dir, wrong place; torch 2.12.0a0+rocm7.13, Python 3.14.4) |
| `~/llama.cpp/` | 597 M | llama.cpp git clone @ `539f24529` (HEAD == deployed build commit), clean tree, `build/` empty |
| `~/.llama.cpp.build.106328/` | 815 M | Leftover **staging clone + build tree** from `02-install-llama-cpp.sh` — see finding F3 (load-bearing!) |
| `~/.local/share/llama-cpp/` | 348 M | Second, older llama.cpp clone (Aug 25) with its own `build/` |
| `~/.local/bin/` | — | Deployed llama.cpp binaries **and** all `libllama*/libggml*/libmtmd*` **and** `test-*` binaries **and** `hf` CLI, mixed together |
| `~/workspace/ml_workspace/ComfyUI/` | 30 G | **Second, stale** ComfyUI clone (@ `71b73e3b`) whose `models/` holds 2 unique files (21.6 G) + 2 duplicates of `~/ComfyUI/models` (9 G) |
| `~/vulkan/1.4.341.0/` | 1.6 G | Vulkan SDK (build dependency) |
| `~/ai-projects/gguf-indexer/` | — | Model-indexing tooling: `gguf-indexer.py` (66 KB, current), `router-presets.py`, `gguf_analyzer/` (Rust), `archive/` (old versions), `venv/` |
| `~/ai-projects/ai-portal/` | — | This project. Contains only `02-install-llama-cpp.sh` so far |
| `~/docker-desktop-amd64.deb` | 433 M | Unused installer junk |
| `~/Downloads/` | 279 M | Wallpaper jpgs, `duck.ai` log dump, an amd-deb |
| Home root | — | ~15 scripts/logs written by other AI models (see §6) |

`~/.cache` total: **278 G** (llama.cpp 244 G + huggingface 31 G + pip 2.7 G + misc).

## 4. Router behavior — confirmed from source (llama.cpp b10641)

Verified in `~/llama.cpp` source (`common/preset.cpp`, `tools/server/server-models.cpp`,
`tools/server/README.md` §"Using multiple models"):

1. **Model sources, cascaded:** (a) HF-cache models (auto), (b) `--models-dir` scan,
   (c) `--models-preset <ini>`. Local dir overrides cache by name; INI merges over both.
2. **`--models-dir` scan is one level deep** (`fs_list(models_dir, true)` = immediate
   children only):
   - each **subdirectory** = one model; **directory name = model id/alias**;
   - inside it: main `.gguf` (or first `-00001-of-` shard for split models),
     sidecars whose name contains `mmproj` → `path_mmproj`, and files starting with
     **`mtp-` / `dspark-` / `dflash-`** → `path_draft` (first wins);
   - a loose `.gguf` directly in the dir = single-file model, **no sidecar association**;
   - loose `mmproj*` / `mtp-*` files are *skipped entirely* (never become models, never attach).
3. **Preset INI** (`--models-preset`): sections are model ids; `[*]` = globals applied to
   all. Valid router-only preset keys include `load-on-startup`, `stop-timeout`,
   `dedup-cache-models`. All regular `llama-server` args (`device`, `ctx-size`,
   `threads`, `fit`, `fit-target`, `mmap`, `mmproj`, `model-draft`, `spec-draft-device`, …)
   work as preset keys.
4. **HF cache**: llama.cpp reads `HF_HOME → XDG_CACHE_HOME → ~/.cache/huggingface/hub`
   (`common/hf-cache.cpp`); cache models appear as `org/repo:tag` entries.
   `dedup-cache-models=1` hides a cache entry whose file a preset already uses.
5. Workers inherit the router's env + CLI args; model id is passed as `--alias`.

## 5. Findings (problems in the current state)

**F1 — Flat model dir breaks helper-model association (high).**
`~/.cache/llama.cpp/models` is flat. Per §4, all five `mmproj-*` files and the
`mtp-Qwen3.8-27B-Q4_0.gguf` draft are **orphaned**: they are not attached to any model.
Live `/v1/models` confirms it — every model reports `input_modalities: ["text"]` only,
even the four that have mmproj files on disk, and the Qwen3.8 worker (this session)
runs without its MTP draft.

**F2 — Laguna (2-shard, 68 GB) is registered as two broken models (high).**
Flat scan registers each shard separately:
`Laguna-S-2.1-Q4_K_M-00001-of-00002` and `…-00002-of-00002` — neither is loadable
(a shard-2-only "model" can't start). The router only understands multi-shard inside a
subdirectory.

**F3 — Deployed binaries depend on a leftover staging dir (high).**
`readelf` shows the deployed `llama-server`/`libllama*` carry
`RUNPATH = /home/cortana/.llama.cpp.build.106328/build/bin:/opt/rocm/lib`.
The libs in `~/.local/bin` are **not** on that runpath — the running/installed
binaries load their shared libraries **from the staging directory**.
Deleting `~/.llama.cpp.build.106328` (815 M) would break every future start of the
current binary. It must stay until a rebuild with a self-contained rpath is deployed
(Phase 3 of the plan).

**F4 — `presets.ini` is dead weight as wired (medium).**
The running router was started **without** `--models-preset`, so the hand-tuned
`presets.ini` (per-model Vulkan/ROCm device picks, ctx caps, fit) is never applied —
the router runs every model with defaults. Also, its section names
(`[laguna-s-2-1-q4-k-m]`, `[qwen3-8-27b-ud-q8-k-xl]`…) don't match the actual model
ids produced by the flat scan, so even if loaded they'd create *duplicate* models
instead of tuning the real ones. (The tuning values themselves — MoE→Vulkan,
dense→ROCm — look sound and are worth keeping.)

**F5 — Duplicate Qwen3.8 entry (low).**
`/v1/models` lists both `Qwen3.8-27B-UD-Q8_K_XL` (models-dir, via symlink into the HF
blob) and `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL` (cache source) — the same 31.5 GB file
twice. Fixable with `dedup-cache-models` + a stable local model id.

**F6 — LM Studio originals still sit in `~/models` (264 G) (medium).**
Size-comparison of all 19 GGUFs shows the flat copy is 1:1 complete **except**
`unsloth/Qwen-Image-2512-GGUF/qwen-image-2512-Q8_0.gguf` (21 G) which was never
copied. **Decision (user):** that image model was an abandoned LM Studio
experiment (LM Studio can't run it; ComfyUI covers the use case), so it is
**excluded** from the canonical store and will simply be deleted when `~/models`
goes. Everything else in `~/models` is a pure duplicate → 264 G reclaimable via
Phase 5 `--remove-lmstudio`.

**F7 — Three llama.cpp source trees, three build trees, one mixed bin dir (medium).**
`~/llama.cpp` (current), `~/.local/share/llama-cpp` (stale Aug-25 clone + build),
`~/.llama.cpp.build.106328` (staging clone + build, load-bearing per F3). Binaries,
shared libs and 40+ `test-*` binaries all dumped into `~/.local/bin` alongside the
`hf` CLI. No versioning, no rollback story.

**F8 — ComfyUI split-brain (medium).**
Repo at `~/ComfyUI` (correct case) but venv at `~/comfyui` (wrong case), and a second
stale clone at `~/workspace/ml_workspace/ComfyUI` whose model dir holds
`qwen_image_2512_fp8_e4m3fn.safetensors` (20 G) +
`Qwen-Image-2512-Lightning-4steps` LoRA (1.6 G) — **unique files that would be lost**
if that folder is simply deleted — plus 9 G of duplicates of `~/ComfyUI/models`.

**F9 — No service management (medium).**
The router runs as a manual foreground terminal process (`pts/1`). No systemd unit
for llama.cpp or ComfyUI → dies on reboot, no restart-on-failure, no logs. The portal
should own these as user services.

**F10 — Home-directory clutter from prior AI sessions (low/medium).**
~15 scripts/logs in `~/` (see §6), 433 M `docker-desktop-amd64.deb`, wallpaper +
log dumps in `~/Downloads`. The two "useful" artifacts (`presets.ini` tuning values,
`INDEX.json`) live *inside* the model dir next to the weights.

**F11 — HF cache is fine, keep it in place (info).**
`~/.cache/huggingface` (31 G) is the default location for `hf` CLI, `huggingface_hub`
(ComfyUI/transformers) **and** llama.cpp's `hf-cache` module. Its blob+snapshot+
`MTP/` subfolder layout is exactly the helper-model subfoldering pattern llama.cpp
expects. Moving it would require env plumbing (`HF_HOME`) in every consumer; no benefit
today. Recommendation: keep, let the portal manage it via `hf` CLI.

## 6. Home-script triage (prior AI model artifacts)

| Script | Verdict |
|---|---|
| `02-install-llama-cpp.sh` (in ai-portal) | Good bones (lock, staging, verify, deploy) but: builds in a per-PID staging clone whose path gets baked into RUNPATH (→ F3), deploys a flat bin dump, never cleans `~/.local/share/llama-cpp`, never wires `--models-preset`. **Superseded by `tools/phase3_rebuild_llama_cpp.py`.** |
| `00-cleanup-shell.sh`, `01-setup-prerequisites.sh`, `02-fix-llama-cpp.sh`, `02-5-test-llama-cpp.sh`, `02-install-llama-fresh.sh`, `03-migrate-models.sh` | One-shot setup/migration history; `03-migrate-models.sh` is the script that produced the flat mass-copy (F1/F2). Archive. |
| `llamacpp-start-model` + `~/.config/llama-cpp/llamacpp-server.conf` | **Broken**: references a nonexistent `llamacpp-server` binary and an LM-Studio-style conf (`GPU_LAYERS=99`, port 8000). Archive/delete. |
| `llamacpp-update` | Legacy update helper; superseded by phase3. Archive. |
| `diagnose-llama.sh`, `diagnose-boot-state.sh`, `boot-diagnostic.txt`, `cleanup-boot-system.sh`, `configure-refind.sh`, `install-refind.sh`, `install-halo-theme.sh`, `verify-and-setup-uefi.sh`, `final-verification.sh`, `backup-before-cleanup.sh`, `rEFInd_GUI/` | Boot/UEFI/theme work, root-required, **unrelated to the AI stack** — leave in place (out of scope). |
| `discover-models.py`, `model_migration_*.log`, `ai_environment_report_*.txt`, `duck.ai_*.txt`, `image.PNG` | Dumps/logs from prior sessions. Archive or delete. |
| `~/models/` | See F6. |

## 7. Model inventory (canonical set, 13 models + 6 helpers)

| Model dir (future id) | Main file(s) | Sidecars | Notes |
|---|---|---|---|
| `Qwen3.8-27B-UD-Q8_K_XL` | 31.5 G (HF blob) | `mtp-Qwen3.8-27B-Q4_0.gguf` 1.4 G (HF blob) | Daily driver, currently loaded |
| `Laguna-S-2.1-Q4_K_M` | 2 shards, 71.1 G | — | MoE; must be one subdir (F2) |
| `Qwen3-Coder-Next-Q4_K_M` | 45.1 G | — | MoE |
| `ornith-1.0-35b-Q5_K_M` | 23.0 G | — | MoE |
| `Qwen3.6-35B-A3B-Q4_K_M` | 21.2 G | mmproj 0.9 G | MoE + vision |
| `ThinkingCap-Qwen3.6-27B-Q8_0` | 27.0 G | mmproj 0.9 G | dense + vision |
| `gemma-4-31B-it-QAT-Q4_0` | 16.4 G | mmproj 1.2 G | dense + vision |
| `muse-glimmer-30B-kquant-17gb` | 15.6 G | `mmproj-kquant.gguf` 1.3 G | dense + vision |
| `Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M` | 5.2 G | mmproj 0.9 G | 1 M ctx trained |
| `stackexchange_literature.Q8_0` | 8.0 G | — | |
| `qwen3-emb-8b-Q8_0` | 7.5 G | — | embeddings |
| `Mistral-Code-Architect-7b` | 4.1 G | — | |
| ~~`qwen-image-2512-Q8_0`~~ | 21.0 G (only in `~/models/unsloth`) | — | **excluded by decision** (F6) — dies with `~/models` |
| `Qwen3.5-0.8B.Q4_K_M` | 0.5 G | — | |

ComfyUI models (92 G, already in the correct category layout under `~/ComfyUI/models`)
plus the 21.6 G unique files from the stale workspace clone (F8) form the ComfyUI store.

## 8. What the portal will inherit (constraints to design around)

1. **Atomic, rollback-able deploys**: router must never be left with a half-deployed
   binary set; old releases must be restorable (F3/F7).
2. **Model store contract**: one subdir per model under a single `--models-dir`,
   sidecars inside (`mmproj*`, `mtp-*`/`dspark-*`/`dflash-*`), dir name = public model id.
   Hard links (same NVMe) make reorgs/copy/move operations O(ms) and free.
3. **Per-model runtime config** must go through `--models-preset` INI (device, ctx,
   fit, stop-timeout, load-on-startup) — validated keys only.
4. **Services**: llama.cpp router (and later ComfyUI) as systemd **user** services.
5. **Updates**: llama.cpp = git fetch + pinned rebuild + staged release + symlink swap;
   ComfyUI = git fetch/pull + venv requirements sync (no build); HF = `hf` CLI.
6. **Never touch `nvme1n1`.**
7. The agent session itself is served through the router (F: port 8080 → worker on
   ephemeral port) — migrations that stop the router must be user-run.
