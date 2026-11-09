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

## 4. Open questions (the cancelled subagent's scope — resume prompt lives here)
Restart protocol: spawn `general` subagent (background) seeded with §1–§3 of this
file, then ask it ONLY:
1. What is Turbo exactly (step-distilled? canonical steps/cfg; relationship to the 5/6/8-step distill loras and the 0.85 G turbo_lora — base+lora ≈ Turbo?).
2. Editing on 2.1: same DiT + Qwen3-VL encoder (TextEncodeQwenImage21 image slot) vs separate Edit model lineage (2509 edit) — what is native, what does ComfyUI core need, fun_controlnet_union usage.
3. RGBA: 2.1 official alpha prompt-wrapper (forge-validated) vs `Qwen/Qwen-Image-Layered` separate model + Layered trio nodes — when is Layered the right tool.
4. Community improvements: texture-fix-vae (madebyollin) reported effect; lightx2v Lightning-v2-RL steps/quality; TailSteer; uncensored quants — which are real "Swift 1.5"-class upgrades with evidence.
5. ROCm/gfx1151 status 2026: bf16 support (known good: z_image bf16 runs), fp8 e4m3fn runs (known: 2512_fp8 runs) — is int8_convrot a no-go on ROCm (bitsandbytes CUDA kernels), w4a8 (Triton; ROCm support?), any Strix Halo qwen-image reports with s/it.
6. Model-sharing law inputs: which quantization families are hardware-portable across BOTH boxes (bf16/fp16 portable; int8_convrot CUDA-sm80+; w4a8 Triton arch; GGUF needs ComfyUI-GGUF, ROCm speed?), so the once-downloaded set.
7. Qwen3-VL 8B TE in ComfyUI core: CLIPLoader type qwen_image expectations, qwen3vl_8b_bf16 RAM/VRAM behavior on 128 GiB unified, PE 9B t2i/i2i node + quality gain evidence.

## 5. Picks (provisional, pending §4; owner GO gates downloads)
- **halo chain** (bf16/fp8 language, huge addressable vram): `qwen_image_2.1_bf16` + `qwen_image_2.1_turbo_bf16` + `qwen3vl_8b_bf16` TE + 2.1 VAE + `texture-fix-vae` candidate + 5/8-step distill loras; PE 9B later (CPU/router prose lane law).
- **forge chain**: already canon (int8_convrot trio + turbo/edit/rgba contracts); candidates = texture-fix-vae, lightning/step loras, w4a8 TE is likely NO (Turing Triton).
- **Downloads** (owner GO): `Comfy-Org/Qwen-Image-2.1` subset via hfgrab spec, chain law ≈ 40–45 G halo set.
- **Sharing law (draft)**: download once = bf16/fp16 files, loras, VAEs (hardware-agnostic). CUDA-only: int8_convrot. GGUF: needs ComfyUI-GGUF node. Big coding LLMs: llama.cpp-only, stay on halo router (forge 2080 Ti can't host them).

## 6. Where this lands
Findings post to Workroom bench `qwen-image-2.1` as posts (agent posts land
quarantined → owner promotes = W0 witness). Comparison-test task sheet for the
owner's forge opencode session lives here once picks firm. Journal: §6.14.1z* in
`docs/PORTAL_PLAN.md`.
