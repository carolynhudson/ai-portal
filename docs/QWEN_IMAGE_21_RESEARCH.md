# Qwen-Image-2.1 — research state (live document)

Status: **IN PROGRESS**. This file is the durable state of the Qwen-Image-2.1
pick/research task, written 2026-11-09 so the work survives context compaction.
The web-research subagent (session `ses_e3e9a7428ffeITaeWg1XPHmjpk`) was
**cancelled** mid-flight to protect the parent context; its scope is captured in
§4 as the resume prompt. Ground truth in §1–§3 is measured, not remembered.

Owner's ask (verbatim intent): research `Qwen/Qwen-Image-2.1` and
`Qwen/Qwen-Image-2.1-Turbo`; pick the best base for halo's setup; find improved
versions/fine-tunes on HF (the "Swift 1.5 over naïve quants" analogy — find the
community fix of the official release); map the toolchains that fully enable
generation, editing, RGBA; plan comparison testing on image-forge via its
opencode v2 harness; decide what to download once and share across devices.
This task is also the first bench (`qwen-image-2.1`) of the Workroom
(`docs/COLLAB_PROPOSAL.md`).

## 1. Measured HF ground truth (2026-11-09, via HfApi files_metadata)

### `Qwen/Qwen-Image-2.1` — 30.9 G, diffusers layout
- `transformer/diffusion_pytorch_model-{00001,00002}-of-00002` = 9.28 + 3.97 G (13.25 G bf16 DiT, ~6.6 B params — 2.1 is the small-DiT line, NOT the old 20B)
- `text_encoder/model-0000{1..4}-of-00004` = 16.3 G bf16 (Qwen3-VL 8B class)
- `vae/diffusion_pytorch_model` = 1.26 G (fp32 VAE)

### `Qwen/Qwen-Image-2.1-Turbo` — 30.3 G
- same transformer pair; `text_encoder/model.safetensors` 16.33 G single-file; **no vae folder**

### `Comfy-Org/Qwen-Image-2.1` — 114.4 G, the one-stop ComfyUI repo
- diffusion_models: `qwen_image_2.1_bf16` 13.25 G · `qwen_image_2.1_int8_convrot` 6.76 G · `qwen_image_2.1_turbo_bf16` 13.25 G · `qwen_image_2.1_turbo_int8_convrot` 6.76 G (NO fp8 variants in this line)
- text_encoders: `qwen3vl_8b_bf16` 16.3 G (+ int8_convrot + w4a8 variants), and **9B prompt-enhancer TEs**: `qwen3.5_9b_qwen_image_2.1_pe_t2i` / `_pe_i2i` in int8_convrot 8.82 G and w4a8 6.26 G
- loras (step distills): `p_q_image_2.1_{5,6,8}_steps` bf16 ~0.31–0.32 G; `qwen_image_2.1_turbo_lora_avg_rank_178_bf16` 0.85 G
- model_patches: `qwen_image_2.1_fun_controlnet_union` bf16 7.03 G / int8_convrot 3.52 G
- (2.1 vae listed at 0.63 G bf16)

### Community finds (HF search "Qwen-Image-2.1", 2026-11-09)
- `lightx2v/Qwen-Image-2.1-Lightning-v2-RL` — Lightning-style speedup, v2 RL
- `madebyollin/texture-fix-vae-for-qwen-image-2.1` — the "fix the naive artifacts" VAE (madebyollin = the int8-convrot author; strong quality signal)
- `Viggle/Qwen-Image-2.1-viggle-turbo`, `GooHAI/Qwen-image-2.1_loras`
- GGUF/quantized community repos: `AtomicChat/Qwen-Image-2.1-Turbo-Uncensored-GGUF`, `chatrlabs/Qwen-Image-2.1-Uncensored-Omni-Quantized` (+LoRA v0.1), `abenzerps/Qwen-Image-2.1-Turbo-Quantized`
- `Lyricccco/Qwen-Image-2.1-TailSteer` — (uninvestigated; sampling-guidance lineage?)

## 2. Measured forge (image-forge) ground truth — 2.1 canon ALREADY validated there
Source: quarantined memories 108–117 (portal DB, `agent:rainbowblock-forge`), read 2026-11-09.

- **Canon 2.1 t2i path (validated on 2080 Ti)**: UNETLoader `qwen_image_2.1_int8_convrot` → QwenImage21Cache (device auto) + CLIPLoader `qwen3vl_8b_int8_convrot` (type qwen_image) + `TextEncodeQwenImage21` (native positive/negative/latent) + KSampler **25 steps, cfg 1, euler/simple**, no shift, no ConditioningZeroOut; negative unused at cfg 1. Renders sharp. Kit contracts: `qwen21_t2i_official` / `qwen21_turbo` (ManualSigmas 8-step) / `qwen21_edit` (same trio + LoadImage refs, `<image1>..`, pixel-budget resolution) / `qwen21_rgba` (OFFICIAL alpha = prompt wrapper "This is an RGBA format image with transparency…", plain PNG out — NO special latent). Birefnet cutout + chroma key contracts validated.
- Portal's legacy recipe (cfg 1.8, shift 1.3, ZeroOut) = z-image-era; SMERS on 2.1 — superseded.
- Measured physics: int8 convrot DiT diffusion 2.2 s/it warm @1024²; 9B PE TE on GPU ≈1.5 tok/s (never); ComfyUI CPU-device LLM decode ≈0.21 tok/s (never). Prose lane = llama.cpp CPU Qwen3-30B-A3B-Q4_K_M `:8081` alias `forge-prose`.
- Forge box: 7950X3D / 64 GB DDR5 / 2080 Ti 11 GB (Turing sm_75: no fp8 TC, int8_convrot is the fast path, nvfp4 = Blackwell-only).
- Forge ComfyUI `:8188` is up; agent server `:24096` (401-gated). Forge-side comparison runs need the owner's opencode session (portal holds no forge credentials by design).

## 3. Measured halo ground truth (2026-11-09)
- ComfyUI 0.43.0, PyTorch 2.12.0a0 + **ROCm 7.13**, device `cuda:0 Radeon 8060S : native`, **108 GiB addressable vram** (128 GiB unified), free 43.8 GiB.
- **custom_nodes: NONE** (only examples) → GGUF family needs ComfyUI-GGUF installed; native nodes only today.
- Library is 2512/2509-era: `qwen_image_2512_fp8_e4m3fn` + `qwen_image_edit_2509_fp8_e4m3fn` run there (fp8 dequant path proven working on this ROCm build), z_image_turbo_bf16, qwen3vl_4b_fp8_scaled + qwen_2.5_vl_7b_fp8_scaled TEs, `qwen_image_vae`. **2.1 needs the Qwen3-VL 8B TE — not present.**
- Migration law applies to every download: hfgrab `-hf` spec, one file per category chain law, GGUF→ladder, Comfy→deepest-folder prior + copy/sha256 verify.

## 4. Open questions — ANSWERED (2026-11-09, main-thread bounded research + forge A/B)

### Q1 — what Turbo is (official card, measured)
- Turbo = accelerated checkpoint of 2.1: **8 denoising steps, CFG 1**, same
  ~7B architecture, loads with `QwenImage21Pipeline`; the 8-step schedule is
  SAVED IN THE CHECKPOINT (`num_inference_steps` alone does not override;
  call-time `sigmas` does). Prefix-KV caching (`use_kv_cache=True`) reuses
  text + reference-image context across steps. Forge measured the equivalent
  ManualSigmas `[1.0, .978, .954, .927, .895, .845, .705, .415, 0]` — same
  schedule, and proved **base + turbo_lora renders turbo behavior** (softer,
  1444 sharpness vs turbo int8's crisp): the halo bf16 chain needs NO
  separate turbo DiT download — bf16 base + turbo_lora is the turbo lane.
- Native resolution is 2K-class: presets 2048², 2400×1792, 2528×1696,
  2752×1536. Editing is the SAME pipeline with `image=` input (single
  sketch→photo, multi-reference grid composition, 4-image interior
  composition all showcased). RGBA ("Transparent Image Generation") is
  NATIVE to 2.1/Turbo — matching the forge-validated prompt wrapper.
- License: Qwen Research License (both). Ecosystem at the repo: 92
  finetunes + 12 adapters + 53 quantizations of the 2.1/Turbo line.

### Q2 — editing (answered by forge canon + card)
2.1 editing is IN the base DiT: `TextEncodeQwenImage21` with image refs
(`LoadImage` → `<image1>..`), pixel-budget resolution; card confirms
multi-reference. No separate Edit model needed for 2.1 (2509 edit-2509
files on halo are the OLD line). ControlNet = the fun_controlnet_union
model_patches (bf16 7.03 G / int8 3.52 G) — not yet wired in a contract.

### Q3 — RGBA vs Layered (answered)
- 2.1/Turbo RGBA = prompt-wrapper alpha, PNG out — native, validated.
- `Qwen/Qwen-Image-Layered` is a SEPARATE model, NOT 2.1-based: 20B DiT
  (old Qwen-Image base), Qwen2.5-VL TE, 50 steps cfg 4, Apache-2.0. It
  DECOMPOSES an image into 3–8 RGBA layers (recursive), for per-layer
  edit/move/resize — a heavyweight adjunct (25 G+ DiT; needs the Layered
  trio nodes, not installed on halo). Path: alpha for generation (2.1),
  Layered later if per-object editing earns its 25 G on halo's vram.

### Q4 — community fixes (forge-quantified, 2026-11-09 A/B, post 1)
- `madebyollin/texture-fix-vae-for-qwen-image-2.1`: **+50% sharpness
  (1931→2890 Laplacian var), identical composition** — keeper, decode-only
  swap, 0.63 G bf16 PORTABLE.
- `lightx2v/Qwen-Image-2.1-Lightning-v2-RL` 4-step: best image of the set
  (3343), 7 s warm — new draft lane on forge.
- Official `p_q_image_2.1_5_steps` uniform-5-sigmas: 10 s, mild distill
  drift. Turbo int8 = mid lane (6 s). Forge lane ladder: lightning4 <
  turbo < pq5 < official-25 (finals).

### Q5 — ROCm/w4a8 (answered for both boxes)
- Forge: w4a8 is a NO (no triton in the Windows torch build); int8_convrot
  is the fast path.
- halo: fp8 e4m3fn proven (2512_fp8 runs), bf16 proven (z_image bf16
  runs); int8_convrot = CUDA-kernel family (NO on ROCm); w4a8 = Triton
  family (no evidence ROCm 7.13 kernels; not in halo's library) → halo's
  language stays bf16 (+fp8 where variants exist; 2.1 ships no fp8).

### Q6 — sharing law (measured, both boxes)
PORTABLE (download once): bf16/fp16 safetensors, all loras, VAEs
(texture-fix 0.63 G, 2.1 vae), fun_controlnet bf16. CUDA-sm80+ only:
int8_convrot (forge keeps its set). Triton-gated: w4a8. GGUF: needs
ComfyUI-GGUF (halo has no custom_nodes — GGUF is a node install away,
and its ROCm speed is unmeasured). Big coding LLMs: llama.cpp-only, stay
on the halo router; forge CPU prose lane = Qwen3-30B-A3B Q4_K_M (20.2
tok/s measured).

### Q7 — prompt enhancement (answered by forge)
9B PE on GPU = 1.47 tok/s (never). PE = prose lane OUTSIDE ComfyUI:
forge → llama.cpp CPU `:8081` (20.2 tok/s); halo → router. Text-enhanced
prompts feed the contracts. ComfyUI CPU-device LLM nodes = 0.21 tok/s
(never).

## 4.1 Method note (owner law 2026-11-09)
This section was built with BOUNDED main-thread research: ~7 targeted
fetches total, each finding written into this doc immediately. No
open-ended subagent. Forge-side facts arrive as promoted Workroom posts
(bench `qwen-image-2.1`, post 1) — the Workroom is the mesh's book.

## 4.2 Raw open-question list (kept for history)

Turbo definition · editing lineage · RGBA vs Layered · community fixes
(texture-fix, Lightning, TailSteer, uncensored quants) · ROCm/w4a8 ·
sharing law · PE encoders. All answered above; TailSteer and the
uncensored quants were NOT adopted (no mandate, no measurement).


## 5. Picks — FINAL (2026-11-09; measurements: forge post 1 + §4)

### halo chain (bf16 language; ~32 G via hfgrab from `Comfy-Org/Qwen-Image-2.1`)
- `diffusion_models/qwen_image_2.1_bf16` 13.25 G — the base; finals at
  25 steps cfg 1 (official params, forge-validated shape).
- `text_encoders/qwen3vl_8b_bf16` 16.3 G — 2.1's encoder (the library's
  qwen3vl_4b/2.5vl fp8 files do NOT serve 2.1).
- 2.1 VAE 0.63 G + `madebyollin/texture-fix-vae` 0.63 G (portable; the
  +50% decode).
- Loras (portable): `qwen_image_2.1_turbo_lora_avg_rank_178` 0.85 G
  (turbo lane WITHOUT the 13.25 G turbo DiT — forge proved base+lora),
  `p_q_image_2.1_5_steps` 0.32 G, `p_q_image_2.1_8_steps` 0.31 G;
  `lightx2v/Qwen-Image-2.1-Lightning-v2-RL` (4-step draft lane, best
  image of the forge A/B).
- Skipped for halo: int8_convrot family (CUDA-only), w4a8 (Triton-only),
  turbo DiT bf16 (lora covers it), PE 9B TEs (prose = router lane, never
  ComfyUI-CPU), fun_controlnet (later, if control work starts).

### forge chain (measured, already installed by the forge session)
- Canon trio (int8_convrot DiT + qwen3vl_8b int8 TE + TextEncodeQwenImage21)
  with **texture-fix vae as the decode vae in every contract**; lane ladder
  lightning4 (7 s) < turbo int8 (6 s) < pq5 (10 s) < official-25 (53 s,
  finals). Contracts: qwen21_t2i_official/_turbo/_edit/_rgba +
  qwen21_t2i_lightning4/_pq5.

### toolchains, settled
- GENERATION: native (both boxes).
- EDITING: native to 2.1 (same trio + image refs); fun_controlnet_union
  available for structural control (bf16 7.03 G on halo if needed).
- RGBA: native prompt-wrapper alpha (validated); Layered (separate 20B,
  Qwen2.5-VL, Apache-2.0) is a per-object decomposition adjunct — halo
  vram can host it later; not in this download.
- Speed: forge = int8 lanes; halo = bf16 (fp8 where variants exist);
  prose/PE = llama.cpp prose lanes on both boxes.

### download law (owner GO to execute)
`Comfy-Org/Qwen-Image-2.1` subset spec (base bf16 + qwen3vl_8b_bf16 +
turbo/pq loras + 2.1 vae) ≈ 31 G, then `madebyollin/texture-fix-vae` +
`lightx2v/Qwen-Image-2.1-Lightning-v2-RL` as two small specs. Chain law
applies; the forge truncation story (570/913 MB "success") is already
covered portal-side by copy+sha256 verify on Comfy lands.

Exact targets (measured via HfApi, 2026-11-09):
- `madebyollin/texture-fix-vae-for-qwen-image-2.1` →
  `texture_fix_vae_for_qwen_image_2.1_bf16.safetensors` 644 M (skip the
  1.29 G fp32 twin).
- `lightx2v/Qwen-Image-2.1-Lightning-v2-RL` →
  `qwen_image_21_lightning_v2_rl_4steps_lora_rank64.safetensors` 656 M
  (skip the 27 G full distilled model — the lora onto the bf16 base is
  the lane; the full model would compete with the base, chain law).


## 6. Where this lands
Findings post to Workroom bench `qwen-image-2.1` as posts (agent posts land
quarantined → owner promotes = W0 witness). Comparison-test task sheet for the
owner's forge opencode session lives here once picks firm. Journal: §6.14.1z* in
`docs/PORTAL_PLAN.md`.
