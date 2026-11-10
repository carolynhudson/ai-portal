# MacBook AI plan — M4 Max 128 GB (research state 2026-11-09)

Goal (owner): the 2025 MacBook Pro M4 Max 128 GB joins the family as a full
AI box — a llama.cpp-router-class server like halo's, switchable model
states, the Swift 1.5 Flash-Next IQ3_S experience if possible, an AI pair-
programming lane (ThinkingCap / Swift Qwen 27B + a fast autocomplete model),
and the box reachable by every harness. Numbers below are community-
measured on M4 Max 128 GB (546 GB/s; ~96 GB comfortable working budget)
unless marked estimate. Bounded main-thread research, owner law.

## 1. Engine law on Apple Silicon (measured 2026)
- Two engines share one unified memory: **llama.cpp (Metal)** and **MLX**
  (Apple's framework; mlx-lm/oMLX/Rapid-MLX/vllm-mlx servers on top).
  Ollama itself switched to MLX on Mac (0.40, Mar 2026).
- Decode: MLX wins MoE and long outputs — matched 4-bit on M4 Max:
  Qwen3.5-35B-A3B MLX 130.2 vs llama 96 t/s class; Qwen3-30B-A3B 4-bit
  llama 90 / MLX 110 (arXiv 2601.19139: vllm-mlx +21–87% over llama.cpp
  on M4 Max). The 27B dense class: mlx-serve 43 t/s vs llama.cpp Q8 15.7;
  MTP ~2x on dense 27B (oMLX).
- Long-context prefill and KV depth favor llama.cpp discipline; MLX's
  known weakness is long-context (>30K) workloads (mlx-lm #763) and
  short-prompt prefill wall-time.
- Format law: **IQ3_S is a llama.cpp i-quant — "MLX IQ3_S" does not
  exist.** MLX quants are 4/8-bit group (and NVFP4, an MLX-only option).
  So: the Swift 1.5 IQ3_S GGUF runs on llama.cpp Metal (and is the SAME
  file halo uses — download once, mesh law); the MLX lane runs 4/5-bit
  equivalents (mlx-community REAP builds of Flash-Next: top-10 routing,
  MTP, 262k context) for maximum speed.
- Flash-Next 125B-A3B on M4 Max 128 GB, measured: llama.cpp UD-Q3_K_XL
  27.3 t/s (32K); UD-Q4_K_XL f16-KV 16.6 t/s @64K falling to 12 @128K
  (KV is the brake — use FA + quantized KV at long context); MLX Q4
  **73.8 t/s @262K ctx** (mlx-serve); oMLX oQ4e 50–70 @128K; EXL3
  (sushi) 57/63. The most-run model on this box, median 57.2 t/s.

## 2. The switchable state (halo parity, by design)
- **llama.cpp router mode** (in llama-server since Dec 2025): one process,
  one endpoint, children per model from `--models-dir` / `presets.ini`,
  on-demand load, LRU evict, `--models-max`, `--no-models-autoload`.
  Every law the portal learned on halo (one level per model, presets
  sections as bindings, sidecars) applies unchanged on macOS. This is the
  primary: the halo GGUF library + presets travel to the Mac verbatim.
- **llama-swap** (Go, one binary): swaps ANY engine (llama-server, MLX
  servers, vLLM), groups/matrix/capacity scheduler (measured-size +15%
  margins), profiles, Prometheus, **MCP endpoint at /api/mcp**, and
  **peers** — a halo+MacBook federation under one endpoint. Adopt when
  the MLX and GGUF lanes must share a single URL.
- **oMLX**: the MLX-native multi-model server (pin/evict, SSD KV cache
  surviving restarts, OpenAI+Anthropic APIs, leaderboard). The MLX lane
  server of choice; author joined Hugging Face (Sep 2026), project active.

## 3. The stack to install (plan)
1. Homebrew + `uv` + python 3.12; git; rsync (model sync from halo).
2. llama.cpp (brew or release, Metal): router mode,
   `--models-dir ~/ai/models/llama --presets ...` — same layout as halo;
   the IQ3_S Swift 1.5 file syncs from halo (never re-download).
3. mlx-lm + oMLX: Flash-Next MLX/REAP lane (73.8 t/s @262K measured) as
   the fast agentic lane; llama.cpp IQ3_S lane as the halo-parity lane.
   Switch via router mode today; llama-swap when lanes merge.
4. Embedder law: `ggml-org/embeddinggemma-300M-GGUF` (the portal RAG
   embedder, portable) on its own always-on slot — small models pin.
5. ComfyUI (MPS) for the image lane — 2.1 bf16 chain is the portable
   quant family (QWEN_IMAGE_21_RESEARCH §5); expect it to fit 96 GB
   trivially; treat as the halo-cousin lane, speed unmeasured here.
6. whisper.cpp for audio (cheap on this silicon).

## 4. Pair-programming lane (measured picks)
- Chat/edit/agent: **Swift/ThinkingCap Qwen 27B RCO GSQ** — 27B dense:
  Q4_K_M ≈ 16.5 G → ~30 t/s class; Q6_K fits (96 GB budget) with
  32–64K; MLX 43 t/s + MTP ~2x is the fast lane. Qwen3.8-27B is the
  field's pick for coding at this tier (Apache-2.0, 256K).
- Autocomplete law (field-verified): FIM **base** models, small,
  sub-second: `qwen2.5-coder-1.5b-base` (the standard), 3B if it must
  be stronger; Qwen3-Coder FIM works in every size — if a Qwen3-Coder
  MoE is already resident, point completion at it for free; Sweep
  Next-Edit 1.5B (open, Jan 2026) for next-edit prediction. Autocomplete
  NEVER from the chat model (latency law).
- Editors: **Zed** (native local, FIM examples), **Continue** (roles:
  chat/edit = 27B, autocomplete = 1.5B-base, embed = embeddinggemma),
  Tabby, llama.vscode. The opencode harness runs natively on macOS —
  its provider points at the local router endpoint (same contract as
  halo; plugin pilot applies).

## 5. Mesh integration (gated by the consent checklist — MacBook law)
- MacBook joins as a portal **agent** (kit token, read/write scopes;
  writes land quarantined) and optionally a second router endpoint;
  llama-swap peers can federate halo+Mac behind one URL.
- Share-once list this plan creates: Swift 1.5 IQ3_S GGUF, presets,
  27B RCO GSQ quants, embeddinggemma, 2.1 bf16 image chain — every one
  hardware-portable between halo (ROCm) and the Mac (Metal) because
  GGUF/bf16 crosses vendors.

## 6. Verdict (one paragraph)
Run **llama.cpp router mode as the spine** (halo's exact law, halo's exact
files, IQ3_S included), **oMLX as the MLX speed lane** (Flash-Next 4-bit:
73.8 t/s at 262K — 2–3x the GGUF lane), **llama-swap as the later
federator** (engine-agnostic, capacity-aware, MCP-visible, peer-aware).
Pair programming = 27B RCO GSQ chat/edit + 1.5B-base FIM autocomplete in
Zed/Continue, embedder pinned. Nothing on this list is CUDA-only; all of
it is download-once with halo.
