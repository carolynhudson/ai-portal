# Image Forge — an agent-facing image toolbox (proposal, 2026-10-25)

Owner framing (verbatim intent): "i don't think i have ever seen or heard of a tool
designed for an agentic ai model … to create and edit images in response to prompting
… by a user in a ai coding harness." The list of features (generation, editing,
scale/crop/metadata) is "a starting point for research and planning, not an exact
MVP." Owner's own pain points to design against: generators that won't conform to
exact pixel dimensions; no true alpha (workaround: chroma-key + external tool);
iteration is visual and sometimes wants undo. Acceptance anchors the owner proposed:
(1) a small logo graphic enhancing/replacing the "AI Portal" header text, (2) a
website icon (favicon set).

Status: proposal for adjudication. Nothing implemented.

## 1. Measured ground (this box, Oct-25)

- **Vision is LIVE, today, on the session model.** The router journal:
  `load_model: loaded multimodal model, '.../mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf'`,
  and `/v1/models` reports the session model's `architecture.input_modalities:
  [text, image]`. llama.cpp's HF-cache preset loader auto-pairs mmproj files
  (`common/preset.cpp: is_mmproj_file` substring rule, per-snapshot), so the
  vision tower has been loaded with the model all along. The portal's cache scan
  sees it too (`scan_cache → has_mmproj=True, has_vision=True, sidecar_type=mmproj,
  907 MB`; 7 vision rows in cache). The blind spot was `INDEX.json`'s library-only
  scope, not the detection. The personality INI format natively accepts
  `mmproj = <path>` overrides (llama.cpp `key_to_opt` maps any llama-server arg);
  portal editor surfacing of that line is a recorded queue item. **The forge's
  visual feedback loop can therefore close in-model from day one** — MCP image
  tool-results → file attachment → pixels (§1 opencode source facts).
- **The memory arithmetic is the real constraint** (measured, session with the
  ~40 GB IQ3_S resident): `free` shows **~10 GiB MemAvailable**. Pipelines:
  qwen-image ≈ 20 GiB diffusion + 8.8 GiB TE + 0.6 GiB VAE + torch overhead
  ≈ **~32 GiB**; z-image-turbo ≈ 12 + 7.5 + 0.6 + overhead ≈ **~22 GiB**.
  Neither fits beside this model — the owner's demand is exactly right, and the
  forge's **load gate** is: refuse any ComfyUI job whose pipeline estimate +
  8 GiB safety exceeds MemAvailable, refusing **with the arithmetic printed**
  (loaded models' bytes, MemAvailable, pipeline need). Path to first gens: run
  the forge session on a small vision-capable model — the box already has
  `Jackrong/Qwopus3.5-4B-Coder-GGUF` **with `mmproj-F32.gguf` in its snapshot**
  (auto-paired by the same rule), ~5 GiB resident → z-image-turbo fits beside
  it with margin. The gate states this path when it refuses.
- **Memory-efficiency contract** (owner requirement, binding on the design):
  single queued job at a time; pre-load gate reading **MemAvailable + swap fill**
  (measured: MemAvailable swings 10→35 GiB with load state; the router model is
  file-backed mmap — evictable under pressure, trading inference speed for RAM;
  ComfyUI weights are anon) — refuse when pipeline estimate + 8 GiB safety
  exceeds MemAvailable, **refusing with the arithmetic printed** (loaded
  models' bytes, MemAvailable, swap state, pipeline need); after every job
  `POST /free {"unload_models": true, "free_memory": true}`; the bench helper
  is short-lived subprocesses (no resident image process); ComfyUI idle stop
  policy (default 10 min); the MCP server itself is stdlib stdio — a few MB.
  The forge NEVER swaps router models (closed-session swaps only, §4 rules);
  when the gate refuses, the stated remedy is a swap at session end, not a
  forced load. Portal hygiene adopted alongside: the embed sidecar becomes
  **load-on-first-request with idle-eject** (setting, minutes; `kb_mcp`
  already degrades to FTS + vectorize queue when it is down).
- **ComfyUI 0.39.0** installed at `~/ai/comfyui`, service `ai-comfyui` (off by
  default — models are not resident, which is the owner's requirement). Its venv
  already carries **Pillow, numpy, scipy, torch**; it lacks cv2/skimage/rembg/
  imagehash/onnxruntime.
- **Model library** (`~/ai/comfyui/models/`): `qwen_image_2512_fp8` (generation),
  `qwen_image_edit_2509_fp8` (instruction editing), `z_image_turbo_bf16` and
  `krea2_turbo_fp8` (fast/turbo), `Qwen-Image-2512-Lightning-4steps` and
  `Qwen-Image-Edit-2509-Lightning-4steps` LoRAs (4-step fast paths),
  `qwen_image_vae` + `ae` (Flux), text encoders `qwen_2.5_vl_7b_fp8` (+ qwen3vl_4b).
  **Qwen-Image-Layered is NOT downloaded** (the RGBA-layer model — §3.4, phase gate).
- **The API surface is measured from the install** (`server.py` routes):
  `POST /prompt {prompt, client_id}` (flat API-format node graph), `GET /history/{id}`,
  `GET /view?filename&subfolder&type`, `POST /upload/image`, `POST /upload/mask`,
  `GET /queue`, `POST /interrupt`, `POST /free` (model unload), `GET /object_info`,
  `GET /system_stats`. Official docs confirm a pure-urllib (no websocket) loop:
  submit → poll `/history/{prompt_id}` → fetch `/view`. Stdlib-only client: feasible.
- **No vision GGUF exists in the scanned library**: every INDEX row is
  `is_vision=False`, zero mmproj files under `~/ai/models/` (library-only
  scan). The HF cache carries them (§1 first bullet) — see correction above.
- **The engine carries images.** Source-verified in opencode v2
  (`packages/core/src/tool/mcp.ts` → `toContent()`): an MCP tool result with an image
  content part becomes `{type:"file", uri:"data:<mime>;base64,…"}` in the tool
  output, and `tool-output.ts` gates by model modality: bytes the model cannot
  receive inline are **written to a retained file and the path returned**. So an MCP
  server can return a PNG in a tool result: vision-capable models see it, text-only
  models get a stated file path. This is the "pass visual state back if the engine
  can handle it" answer — the engine already does the right thing per model.
- **Aperture discipline** (PORTAL_PLAN model-tree work): qwen-image fp8 (~20 GiB) +
  qwen2.5-vl 7b fp8 (~8.8 GiB) + torch overhead ≈ ~30 GiB against the 108 GiB
  aperture, shared with whatever the router holds. Generation must be **on-demand
  with `/free` after**, never a resident service.

## 1b. Quant-pipeline research + full-box memory audit (Oct-25, round 2)

Owner asks: are there quant-tuned generation/editing pipelines that keep quality
while shrinking the load, how much does the portal stack itself cost, and can a
full coding-agent loop (big model resident + generation) work on this box?

**Pipeline costs (GiB, weights; +3-5 GiB torch/ComfyUI overhead each):**

| pipeline | diffusion | text encoder | VAE+LoRA | ≈ total | status |
|---|---|---|---|---|---|
| qwen-image-2512 fp8 | 20.0 | 8.8 (qwen2.5-vl fp8) | 0.6 | ~33 | **on disk** |
| qwen-image-edit-2509 fp8 | 20.0 | 8.8 | 0.6 | ~33 | **on disk** |
| z-image-turbo bf16 | 12.0 | 4.9 (qwen3vl-4b fp8) | 0.6 | ~22 | **on disk** |
| qwen-image-edit-2511 GGUF Q4_K_M + Lightning-4step | 13.1 | 4.4 (Qwen2.5-VL-7B GGUF Q4_K_M, vision tower included) | 0.3+0.85 | ~22-23 | download gate |
| same, Q3_K_M (dynamic: first/last blocks kept Q8) | 9.8 | 4.4 | 0.3+0.85 | ~19-20 | download gate |
| z-image-turbo GGUF Q4 (community quants exist) | ~4.7 | ~2.6 | 0.6 | ~12-13 | availability check |
| **Qwen-Image-2.1 int8_convrot** (native t2i+edit+**RGBA**+extract; 8-step Turbo is a separate ckpt) | 7.3 | 4.5 (Qwen3-VL-8B int8/w4a8) | 0.25 | **~15-17** | **PRIORITY download** |
| **Qwen-Image-2.1 Turbo** int8_convrot (8 steps) | 7.3 | 4.5 | 0.25 | **~15-17** | priority download |
| Qwen-Image-2.1 GGUF **Q4_K_M** | ~4.6 | 4.5 (int8 TE fine) | 0.25 | **~11-12** | priority download |

**Qwen-Image-2.1 (owner-flagged, Oct-25 — verified against the HF card + docs):**
the "strangely small" is the point — it is a NEW compact architecture: 7B
visual component (32 single-stream DiT layers, mixed-granularity attention,
prefix-KV reuse), not a shrunk 20B. ONE model = text→image, instruction
editing (up to 10 reference images; local edits via **circles / painted
annotations / separate masks**; identity preservation), **native RGBA
generation + transparent-layer editing + subject extraction from photos** —
the unified engine the forge was going to assemble from four pipelines
(gen / edit / layered / chroma-keying). Official ComfyUI support from day
one (templates + **Turbo 8-step** checkpoints; official Comfy-Org package
ships bf16 + int8_convrot diffusion models — the same int8-convrot family
as krea2 on this box — + Qwen3-VL-8B TE variants + dedicated 2.1 VAE).
Community quants: 156 repos already (GGUF Q2→Q8; Q8_0 7.6 G, Q4_K_M ~4.6;
Unsloth: **INT8 > FP8** by LPIPS, FP8 runs at 6 GB with offload, Q4+Q4-TE
runs CPU-only in 12-16 GB RAM → trivial for this box's aperture). CFG 1.0,
euler 25-40 (base) / 8 (Turbo). Native sizes to 2048²+; guidance says
**multiples of 32** (VAE 16× — §3.4 updates to 32 for 2.1).
Caveats, stated: **qwen-research license** (non-commercial research — fine
for this personal box, recorded on the inventory's license line like every
model); "native transparency" is a model-quality claim → the bench's alpha
probes (§3.3) verify every RGBA generation numerically, keying stays for
photos/legacy paths; Q4 _K quants on diffusion are loader-sensitive
(reports of slower _K-path compute on some GGUF forks — measure at T0;
Q8_0/int8_convrot is the fast-safe tier, Q4 the RAM tier).

Quant ground truth (city96/QuantStack/unsloth; GGUF diffusion needs the
**ComfyUI-GGUF** custom node — a supply-chain decision, see §6): Q8_0 ≈
near-original, Q4_K_M "excellent quality, widely recommended", Q3_K_M good,
Q2_K "somewhat usable" — the dynamic quant keeps first/last transformer blocks
at Q8 (community patch notes + city96's imgsli comparisons); that is the
"clever tuning" available. Qwen2.5-VL (not Qwen3-VL) is the TE for qwen
pipelines; GGUF TE includes the vision tower edit needs. The Lightning
4-step LoRAs for 2512/2511 exist (box has the 2509/2512 pair). HiDream-E1.1
(instruction editing) measured 22 GB/768²/28 steps + a 14 GiB TE stack — no
advantage over the qwen GGUF path; skipped.

**Full-box audit (live, cgroup v2 + ps):**

| consumer | RAM |
|---|---|
| router unit (IQ3_S loaded: anon KV/buffers 16.5 G + 40 G file-backed mmap pages) | 16.5 anon |
| portal (uvicorn) | 0.5 G |
| harness (opencode serve 2.1.4) | 0.3 G |
| **ai-embed sidecar** | **0 G — inactive** (RAG MCP stdio child is a few MB; embedding was never the hog) |
| desktop (firefox ~2 G, gnome ~1.5 G, snap-store 0.5 G) | ~4 G |
| swap: `/swap.img` 7.9/8 G used, swappiness 60 | pressure history, not a leak |

Key mechanics: the router's 40 GB model is **file-backed mmap** — the kernel
evicts those pages under pressure (disk re-reads = my inference slows during a
generation, and swap absorbs old desktop pages). ComfyUI's weights land as
**anon** memory. So "generation beside the big model" trades speed for RAM by
design, and the gate must read **MemAvailable** (measured swinging 10→35 G
with load state) + swap fill, not `free`-`used`.

**Verdicts (the owner's questions, answered with arithmetic):**

1. *"Unload the embed model to fit a small generation?"* — embed is already
   unloaded (0 G). With IQ3_S resident right now: MemAvailable ~35 G ≥
   z-image-turbo ~22 G + 8 G safety → **gate ALLOWS a z-image generation today,
   zero downloads, this session's model resident**; qwen-image (~33 G + 8) is
   refused by ~6 G. The embed model never was the blocker.
2. *"Lazy-load MCP services with idle ejection?"* — adopted for design:
   the embed sidecar goes load-on-first-request + idle-eject (portal setting,
   minutes, 0=always-on); `kb_mcp` already degrades to FTS + a vectorize queue
   when the sidecar is down, so the lazy tier is safe today. Cheaper tier
   recorded: embeddinggemma-300m (~0.6 G, an NPU build is already in the HF
   cache) for a resident embedding option.
3. *"Full coding loop needs a second machine, or qwen3.8-27b as primary?"* —
   **this machine suffices for the 27B pairing**: Q8_K_XL-27B (~31 G,
   file-backed; the 27B family snapshots carry their mmproj → vision) +
   qwen-image-edit Q4 (~22 G) + infra ~4 G + safety ≈ ~57 G of 122 G:
   fits with ~60 G headroom. Even IQ3_S + z-image-turbo passes the gate today.
   A second machine buys speed, not feasibility.

## 1c. Remote forge backend — the 7950X3D / 2080 Ti box (Windows 11, often idle)

Owner's second machine: Ryzen 7950X3D (16c Zen4, AVX-512, 96 MB L3), 64 GB
DDR5, **RTX 2080 Ti — 11 GiB** (Turing: strong fp16, no fp8/bf16 compute —
fp8 *weights* dequantize to fp16, which is fine), LAN, mostly idle. The
forge architecture takes it natively: the engine backend is the ComfyUI HTTP
API (`/prompt`, `/history`, `/view`, `/upload/image`), so "remote ComfyUI" is
**a config value** (endpoint URL) — the MCP server stays a local child of the
harness, workspaces stay local, the gate reads the remote's `/system_stats`
for its arithmetic.

Fit table (11 GiB class, weights; fp16-optimized, 2509-era evidence from the
GGUF community):

| pipeline | VRAM need | verdict on 2080 Ti |
|---|---|---|
| **Qwen-Image-2.1 GGUF Q4_K_M** (4.6) + Qwen3-VL-8B TE int8/w4a8 (4.5) + 2.1 VAE | ~10-11 peak | **fits** — unified gen+edit+native RGBA on the Ti (Q8/int8 tier ~13 needs block-swap; Turing HAS int8 tensor cores, so the official int8_convrot tier is fast, not just compact) |
| **z-image-turbo fp8** (6.5) + qwen3vl-4b fp8 TE (4.9) + vae | ~10-11 peak | **fits tight**; 4-step turbo — the fast path, estimate 10-20 s/gen |
| **qwen-image-edit Q3_K_M GGUF** (9.8) + Qwen2.5-VL Q4 TE (4.4, streaming) + lightning-4step | ~10-11 with GGUF streaming | **fits** with ComfyUI-GGUF; estimate 25-40 s/edit at 4 steps |
| qwen-image-edit Q4_K_M GGUF (13.1) | 13.1 > 11 | **hybrid block-swap** — ComfyUI's built-in block-swap is the diffusion analogue of llama.cpp's `--n-gpu-layers` MOE offload (the owner's "hybrid with minimal swapping" pattern, directly): ~1.5-2× slower, fits |
| qwen fp8 safetensors (20 G) | 20 | no |
| krea2_turbo fp8 (13 G), FLUX.2 class | 13+ | no |
| anima-preview3-base (3.9) + T5 | ~6-7 | fits with room |
| SDXL/SD3.5-Medium fp8 | 5-7 | fits (weaker editing story; not the target) |
| CPU-only z-image on the 7950X3D | RAM | ~2 min/gen — the no-VRAM fallback |

The Strix Halo keeps what it is best at (256-bit LPDDR5X bandwidth for the
LLM + the big-RAM quant pipelines + the deterministic bench); the 2080 Ti
contributes what it lacks (CUDA fp16 tensor throughput). Security: ComfyUI
answers **unauthenticated** — the remote bind must be LAN-interface with a
Windows firewall rule scoped to the portal box (192.168.1.228), stated in the
setup script; the harness is already LAN-exposed on the same trust domain, so
this matches, not extends, the current exposure.

**Test protocol (owner: skeptical, open to testable ideas) — T0 is zero-cost:**
1. Windows box: portable/green ComfyUI + ComfyUI-GGUF node; download
   z-image-turbo fp8 (6.5 G) + qwen3vl-4b TE + vae + 2511-Lightning LoRA;
   `--listen 0.0.0.0`; firewall rule inbound 8188 scoped to LocalAddress
   192.168.1.228 (owner runs the admin commands).
2. Portal box: `GET http://WIN:8188/system_stats` (network + version + VRAM
   facts), then run the forge's own frozen templates against it — z-image
   1024²/4-step, qwen-edit Q3 + lightning, qwen-edit Q4 + block-swap —
   recording **s/gen and peak VRAM from /system_stats**. The remote gate gets
   measured numbers, and P1 anchors (logo/favicon) generate on the remote
   backend if T0 passes.
3. Side interest (the "useful compute" question, cheap to measure): llama.cpp
   on Windows with a MoE hybrid — Qwen3-30B-A3B-class Q4 under 64 GB RAM with
   experts on CPU / attention on the 2080 Ti — a t/s benchmark tells the owner
   what that box is really worth as an LLM peer; recorded as a probe, not a
   commitment (the portal's router-model rules stay: it is not the portal's
   router).

## 2. Prior art — how image tools are built for callers (2026)

The design bar, from the tools the owner has used and the current APIs:

1. **OpenAI GPT Image API** (docs, 2026): fixed size presets (`1024x1024`,
   `1536x1024`, `1024x1536`, "auto", >2560x1440 experimental) — **exact dimensions
   are solved by refusing free-form sizes**; `quality: low|medium|high` (the
   one-shot↔iterate dial); `background: transparent|opaque|auto` with
   png/webp output — **alpha is a first-class parameter**; edits take
   `mask` = image whose **alpha-zero regions** mark the edit area (mask shares
   format+size with the input); `input_fidelity: high|low` (preserve input
   features). Community-verified weakness (openai forum threads): mask compliance
   is soft — prompts fight masks; outputs drift sizes. That is the gap a
   deterministic layer must close.
2. **Stability Studio API**: named **ops** over an image — `remove_background`,
   `inpaint`, `outpaint`, `erase`, `replace_subject`, `search_replace`,
   `background_adjust`, `style_transfer` — plus control endpoints
   (structure/depth/sketch) each with a `control_strength`. Verb-per-op reads
   agent-friendly.
3. **Firefly/Generative Fill**: selection = mask, **iterative passes** with
   strength; the human loop is act → look → re-act. The loop, not the single shot,
   is the product.
4. **Qwen-Image-Layered** (Qwen team, 2025-12, Apache 2.0, ComfyUI built-in nodes):
   decomposes an image into **`layers+1` RGBA images** via a 4-channel VAE —
   "the first general-purpose method to handle transparent images" natively; edits
   are applied per-layer "physically isolating" them. Measured cost on the web:
   ~45 GB peak / 120 s at 1024px on a 96 GB card → fits this aperture on-demand,
   slow. Not downloaded; it's the alpha answer worth a phase gate.
5. **ComfyUI as a backend service** (docs + 2026 guides): fixed-template API
   workflows, queue polling, `/free` for model eviction, `/interrupt` for cancel.
   Production users ship **frozen workflows + parameter injection**, never
   agent-authored graphs.
6. **Agent visual loops** (computer-use, coding-agent vision): the loop is
   act → screenshot → reason. MCP image tool-results exist (spec:
   `content:[{type:"image",data,mimeType}]`), and opencode turns them into model
   attachments per modality (§1). With a text-only model, the loop must be
   **numbers + ASCII instead of pixels**: probe the image deterministically
   (dimensions, alpha coverage, dominant colors, edge profile, border uniformity)
   and let the agent iterate on measurements.

## 3. Design

### 3.0 Principles (each traces to a measured gap)

1. **Exact pixels by construction, never by asking.** Diffusion models work in
   multiples of 16 (VAE 8× × patch 2×); generation runs at the nearest legal
   size, and the deterministic bench delivers the exact target (fit/crop/pad).
   The agent tool signature accepts exact px and delivers exact px.
2. **Alpha is data, not luck.** Generation composites on a chroma key and the
   bench keys it deterministically (numpy); Layered (§2.4) is the research path
   to native RGBA. The tool layer exposes `background:"transparent"` semantics
   like OpenAI's parameter, hiding the pipeline.
3. **The loop is the product.** Canvas + versions + undo, previews, probes,
   seeds; one-shot quality mode and draft turbo mode are the quality dial.
4. **Text-first visual feedback.** Today's loop runs on measurements and ASCII
   previews; image content blocks ride along so a vision-capable model (a future
   personality, §7) gets the pixels with zero protocol change.
5. **Machine safety stays**: on-demand ComfyUI (confirm-gated start, `/free`
   after, never auto-starts by itself mid-turn; the 108 GiB aperture is shared
   with the router); fixed workflow templates (agent cannot author graphs —
   the plugin supply-chain lesson); outputs live in a bounded workspace; every
   verb audits to the portal db.
6. **Zero new portal dependencies**: the MCP server is stdlib JSON-RPC over
   stdio (the `kb_mcp.py` convention); all pixel work runs in a helper under
   the ComfyUI venv's python, which already has Pillow/numpy/scipy.

### 3.1 Architecture

```
opencode harness (MCP local, seeded like portal-memory)
   └─ image_mcp.py  (stdlib stdio JSON-RPC; session-scoped workspace; audit)
        ├─ forge: fixed ComfyUI workflow templates → POST /prompt → poll
        │         /history/{id} → GET /view → workspace file   (loopback :8188)
        ├─ bench: subprocess → ~/ai/comfyui/venv/bin/python bench.py
        │         (Pillow/numpy ops: fit/crop/alpha/composite/quantize/metadata)
        └─ portal: GET /system_stats (pressure), systemctl --user start/stop
                   ai-comfyui behind confirm=true, db.event audit
```

Workspace: `~/ai/state/forge/<session-dir>/` — `canvas/<id>.json` (manifest:
versions list, actor, tool, args, seed, timestamps) + `v007.png` etc. Files are
the state (the harness restarts; in-memory canvases die — the journal has that
lesson), the **manifest is the canvas**, versions/undo are pointers. Agents never
see paths — canvas ids. Bounded (LRU-cleaned, size cap), audited.

Workflow templates (repo-versioned JSON, API format): `t2i_qwen` (2512 +
Lightning 4-step for drafts / full steps for finals, `background` node wired,
chroma color injected), `edit_qwen` (2509 + its Lightning; mask via
`/upload/mask` + inpaint wiring), `t2i_turbo` (z-image 2-step drafts),
`layered_qwen` (phase 2), `inpaint_edit` (mask region only).

### 3.2 Tool surface (nine tools — MCP context has a real cost; opencode docs
say so, so this is one surface, verbs inside, not fifteen tools)

| tool | args | returns |
|---|---|---|
| `forge_status` | — | ComfyUI up?, resident models, /system_stats pressure, workspace size. Text. |
| `comfyui_start` | `confirm:true` | starts managed unit; audit. Refuses without confirm. |
| `comfyui_stop` | `confirm:true` | `/free` then stop; audit. |
| `gen` | `prompt, w, h, quality:"draft"|"standard"|"final", background:"color:<hex>"|"transparent"|"opaque", seed?, n?, style_hint?` | new canvas version + probe + preview. Draft = turbo/4-step; final = full steps. "transparent" = chroma gen + deterministic key (§3.3). Reports the gen size used + the fit applied. |
| `edit` | `canvas, instruction, mask? (rect / canvas-region / path), seed?, strength?` | new version (qwen-edit 2509). Mask convention mirrors OpenAI's: alpha-zero = edit here. |
| `op` | `canvas, op, args…` — registry: `scale fit cover pad crop trim anchor rotate flop flip composite flatten alpha_key alpha_extract alpha_set alpha_fill despill quantize palette tint strip_meta embed_meta convert mark_rect round_corner` | new version + probe of what changed (e.g. alpha coverage before/after). |
| `inspect` | `canvas, probes?` | dims/format/bytes; **alpha coverage histogram + edge fringe check**; border uniformity (chroma verify: "background reads #FF00FF ±d: 99.7%"); dominant colors; luminance histogram; transparent-bleed check; phash (pure-py, vendorable); EXIF/tEXt/ICC extract. The agent's eyes today. |
| `preview` | `canvas, size?, mode:"ascii"|"image"` | `ascii`: luminance/three-tone text render of the thumbnail — arrives inline, works with text-only models. `image`: a **webp/png content block** — opencode attaches it for vision models (modality-gated, source-verified §1). Both include the path. |
| `canvases` / `undo` / `redo` / `save` | `canvas, to?`, `path?, meta?` | list canvases/versions; pointer moves (undo is instant — the versions are files); save copies out (optional metadata embed) and states the byte count. |

Plus `forge_help(topic)` for the self-docs (icon recipes, chroma recipe, mask
conventions) — the cheap, journal-style answer to "what are the expectations".

### 3.3 The alpha pipeline (the owner's workaround, productized)

**Qwen-Image-2.1 changes the primary path** (§1b): it generates **native
RGBA** (prompted "This is an RGBA image with transparency… background is
transparent"), edits transparent layers, and extracts subjects from photos.
The pipeline becomes: `gen background:"transparent"` → 2.1 native RGBA →
**bench verification** (coverage %, fringe count, border alpha histogram —
model claims get numbers before the canvas accepts them) → `op` fixes
(feather/despill/fill-hole) as needed. The chroma-key pipeline below stays
as the fallback tier: photo sources, the older on-disk models (2509/2512),
and as the **independent verifier** for native alpha (gen the same asset on
magenta, key it, compare alpha vs the native's — when the two agree, the
alpha is trustworthy; when they don't, the bench's numeric probes decide).

1. `gen … background:"transparent"` → template prompts for a **solid #FF00FF
   backdrop** (magenta: far from skin/earth/greens, standard keying color; the
   prompt carries "flat solid background, no shadows, no gradient, object with
   fine hair/fur edges" phrasing) at gen-legal size.
2. bench `alpha_key`: numpy **CIE Lab ΔE** distance to the key (perceptually
   uniform, unlike RGB Euclidean), soft ramp (feather) between `t_lo`/`t_hi`,
   **despill** (magenta spill: pull R/B toward G at edges; the green-spill
   recipe `g=min(g,(r+b)/2)` is the classic analogue), alpha **premultiplied**
   before any resize, unpremultiplied after (kills the dark-fringe artifact
   every naive RGBA resize produces).
3. `inspect.probes=["alpha","fringe","border"]` closes the loop numerically:
   coverage %, fringe-pixel count, border purity — the agent iterates on
   measurements (`t_lo/t_hi/despill`) until the numbers pass, then `save`.
4. Phase-2 research path: Qwen-Image-Layered (native RGBA layers) replaces step
   1 for hard cases (hair, translucency) behind a download + aperture gate.

### 3.4 Exact dimensions

`gen(w=300,h=150)` → template rounds to a legal gen size preserving aspect
(512×256 → 1024×512 at 2× for quality), generates, then `fit`/`cover`/`pad`
deterministically to exactly 300×150; `inspect` confirms. Tools accept exact
px; the model never sees the exact px. This is the OpenAI "fixed presets" idea
with the bench as the enforcement. The legal rounding is **per-model**:
16-multiples for the 20B qwen family / z-image, **32-multiples for
Qwen-Image-2.1** (VAE 16× + patch, official guidance says multiples of 32) —
the template registry carries each model's rule, so the tool surface never
changes.

### 3.5 Visual state back to the caller — three channels, one protocol

- **Numbers** (`inspect`) — always, the text-only loop's eyes.
- **ASCII preview** (`preview:ascii`) — inline, cheap, immediate "is the
  composition right" signal for a text model (luminance-accurate at 60×30 is
  shockingly usable for layout/color-block judgment).
- **Image content block** (`preview:image`) — the real thing; opencode attaches
  it when the model can receive it, else writes a file and returns the path
  (source-verified §1). Zero protocol change when the box gains a vision model.

## 4. Acceptance anchors (the owner's proposed tests)

1. **Portal header logo**: replace/augment the "AI Portal" text in the header
   with a compact mark — target ≤ ~180×36, RGBA clean edges, ≤ ~10 KB, legible
   on the portal's dark theme, `alt` text kept. Exercises: gen (style toward
   flat/geometric), key, exact fit, quantize, byte budget, `inspect` acceptance,
   then the (trivial) template change + existing UI contract tests.
2. **Favicon set**: `favicon.ico` 16/32/48 (multi-size), `apple-touch-icon`
   180, `icon-512.png` with maskable safe-zone (content inset ≥10%), all from
   one master. Exercises: exact sizes ×N, alpha, ICO multi-frame (Pillow),
   deterministic palette, metadata strip.
Both anchors land on a real, shippable artifact — not a throwaway demo.

## 5. Phases

- **P0 bench-only** (no ComfyUI, no downloads): workspace/manifest/undo,
  `op` registry, `inspect`, `preview:ascii`, `save`, MCP server (stdlib) seeded
  into the harness like portal-memory. Verifiable with hand-made PNGs; the
  anchor prep (palette, safe-zones) runs here.
- **P1 forge**: templates + `gen`/`edit` + lifecycle (`confirm` start, `/free`,
  idle-stop policy) + audit + `preview:image` blocks. Anchors 1+2 end-to-end.
- **P2 alpha research + seeing-harder**: Qwen-Image-Layered decision (download +
  aperture + timing measurement on this box), rembg CPU evaluation (gate:
  BiRefNet CPU latency), optional cv2/imagehash/piexif into the comfy venv,
  and the **vision session pairing** (measured §1): forge sessions run on a
  small mmproj-paired model (Qwopus-4B-Coder has `mmproj-F32.gguf` in-snapshot;
  ~5 GiB) so the pipeline fits the gate; portal-side queue item — surface the
  `mmproj = <path>` personality line + vision facts from the cache scan in the
  models tree/editor (INDEX.json's library-only scope is the known blind spot).
- **P3 portal surface** (recorded, not built): a gallery pane (canvas
  versions as history), Status-page footprint, per-personality tool scoping.

## 6. Decisions the owner owes

1. Chroma key color default (`#FF00FF` proposed) and despill defaults.
2. ComfyUI idle policy: stop after N idle minutes (proposed: 10) vs manual.
3. P2 downloads: Qwen-Image-Layered (~20 GiB) + rembg models; vision GGUF
   (qwen3-vl class + mmproj) for in-loop seeing — model-swap rules apply
   (closed sessions only, portal's add-model flow).
4. Tool naming: `image-forge` server, verb tools as above (context cost: ~9
   tool definitions accepted?).
5. Per-personality scoping: all agents get the tools, or an "artist" personality?
6. **ComfyUI-GGUF custom node** (city96): the Q4/Q3 pipeline path needs it —
   it is one widely-used node package, and the proposal's supply-chain rule
   says frozen templates, no nodes. An explicit, measured exception for this
   one package is the cleanest shape; without it the fp8 on-disk pipelines
   are the only ones the forge runs.
7. **Download gate** (§1b table): **Qwen-Image-2.1 is the priority** —
   official Comfy-Org package: `qwen_image_2.1_int8_convrot` + Turbo int8
   (8-step) + Qwen3-VL-8B int8 TE + 2.1 VAE ≈ **~27 GiB of downloads** for
   the full unified pipeline (base + turbo) at ~15-17 GiB RAM each, with
   GGUF Q4 (~4.6 G) as the light tier; it unifies gen, edit (masks/
   annotations/10 refs), **native RGBA**, and subject extraction. The
   2511-Q4 qwen-edit path and Layered (~45 G, decomposition niche) drop
   behind it. z-image-turbo stays as the zero-download first bring-up.

## 7. Honest limits (stated up front)

- The session model IS vision-capable (measured §1) — the visual loop closes
  in-model **when the model's context carries the image**; what remains true is
  that small quants see small details poorly: composition/color/alpha judgment
  is reliable for IQ3-class vision, fine text rendering (the "malformed text"
  failure) is judged best by crop-preview zooms, and the `inspect` numeric
  probes stay the ground truth for exactness. A 27B/vision-strong model improves
  this; the swap rules own that.
- qwen-edit-2509 compliance and mask adherence are model qualities, not API
  guarantees (community evidence: soft mask compliance everywhere, OpenAI
  included). Seeds + drafts + deterministic repair (`op`) is the mitigation.
- ~30 GiB transient loads are aperture-mates of the router's resident set:
  `/system_stats` pressure is reported with every gen and a threshold refuses
  a load, stating why (the portal's peak-RAM discipline, extended).
- ComfyUI workflow JSON is format-drift-prone (node renames between versions);
  templates are versioned with the repo and a live `/object_info` smoke test
  guards upgrades.

## 8. Sources

Measured: this box (ComfyUI server.py routes, models tree, INDEX.json vision
flags, comfy venv imports, model tree aperture). opencode source:
`packages/core/src/tool/mcp.ts` (toContent → file part for MEDIA mimes),
`packages/core/src/tool-output.ts` (modality-gated inline → retained file path);
issues 21227/55072 (image tool results are a live, evolving engine feature).
API docs: OpenAI images (transparent bg, alpha-zero masks, size presets,
input_fidelity — developers.openai.com, 2026); Stability Studio op list;
Qwen-Image-Layered (qwen.ai blog 2025-12-19, docs.comfy.org tutorial, comfyui-wiki
45 GB/120 s figure); ComfyUI comms routes + api-examples (docs.comfy.org:
/prompt client_id, /history, /view, /free, urllib-only pattern).
