# llama.cpp on RainbowBlock - build + tuning law (mini-halo, Windows side)

Pattern mirrors halo (portal KB PORTAL_PLAN sec 4.4): fetch -> build at a ref
-> stage self-contained release -> verify -> swap with backup -> smoke.
Driver: C:\forge\llama\update_llama.ps1 (ON DEMAND - do not schedule).
    powershell -ExecutionPolicy Bypass -File C:\forge\llama\update_llama.ps1            # stage only
    powershell -ExecutionPolicy Bypass -File C:\forge\llama\update_llama.ps1 -Swap      # deploy + smoke
Staged releases: C:\forge\llama\releases\llama.cpp-<sha>\ ; the active dir
(C:\forge\llama) keeps *.pre-<stamp>.bak of every swapped binary (rollback =
copy back + restart the task).

## One build, BOTH tuned paths (owner direction 2026-11-12)

| knob | value here | why |
|---|---|---|
| GGML_NATIVE | ON | compiles the AVX-512 path for THIS host (Ryzen 9 7950X3D = Zen 4; MSVC 17.14 /arch:AVX512 is complete). Binaries are host-locked by design - that is the point of a node-local build. |
| GGML_CPU_ALL_VARIANTS | OFF | runtime dispatch is a Linux-side feature (GCC/Clang per-file targets); on one tuned host NATIVE IS the tuning. |
| GGML_CUDA | ON when a CUDA toolkit is installed | GPU path in the SAME binary. |
| CMAKE_CUDA_ARCHITECTURES | 75 | RTX 2080 Ti = Turing sm_75 ONLY - no fat binary. Driver 617.42 runs CUDA 13; sm_75 is still supported in CUDA 13 (Maxwell/Pascal/Volta were dropped, Turing stayed). |
| llamafile / cuBLAS | defaults ON | im2col GEMM is the CPU win; cuBLAS HIGH is the CUDA default. |
| Vulkan | OFF | CUDA covers the card; one GPU backend keeps the build honest. |

Runtime lane switch, NO rebuild: `-ngl 0` = CPU lane (the router's law today);
`-ngl 99 --device cuda` = GPU lane; `--device cpu|cuda|list` per launch;
`--cpu-moe / --n-cpu-moe N / --override-tensor` for hybrid placement.

## GPU physics (2080 Ti, MEASURED 2026-11-12 on build 8ca5a80 CUDA - the lane map)

- 11.3 GB VRAM, fp16 tensor cores, **no bf16** (Turing), PCIe 3.0 x16 ~12 GB/s.
- MEASURED (llama-bench, -ngl 99, this build): Qwen3-4b Q4 tg **128 t/s** /
  pp256 **3723**; Qwen3-8b Q4 tg **75.9 t/s** / pp256 **2193**. CPU lane for
  the same files: 19.3 / ~11 tg. GPU = 6.6x decode for resident Q4s.
- HOME LANE on GPU: Q4 models <= ~8 GB fully resident with KV headroom
  (Qwen3-4b/8b class). gpt-oss-20b MXFP4 = 11.3 GB file: NO fit with KV.
- NOT the GPU lane: the big MoEs. Arithmetic: flint streams ~2.5 GB of
  expert weights per token; over PCIe that is ~4.8 t/s vs 19 t/s native CPU.
  nemotron (Mamba-2+MoE, ~3.3 GB active) similar. Big models stay CPU by
  bandwidth law, not taste.
- GPU + ComfyUI share the card: LLM GPU lanes must obey the idle law
  (-ngl fits, unload when idle) or the forge render queue wins (VRAM
  contract --disable-smart-memory). Router presets STAY -ngl 0 (CPU) until
  owner/portal decide the VRAM-sharing policy; the CUDA backend loads and
  idles at ~0 (measured: models need only ~1.0-1.3 GB VRAM at ngl 0,
  262k ctx, KV on CPU).
- CUDA RUNTIME PATH LAW (found 2026-11-12): ggml-cuda.dll imports
  cublas64_13/cublasLt64_13, which live in the toolkit **bin\x64** (CUDA 13
  layout; not bin, not on PATH after install). Missing -> process dies
  0xc0000135 at load, or CUDA backend silently fails LoadLibrary (CPU
  fallback). start_forge_router.ps1 now prepends <toolkit>\bin\x64 + bin.
  Any future launcher (meshd, lanes) MUST do the same.

## CPU physics (7950X3D, measured law)

- `-t 16` is the law (physical cores; 32 threads = bandwidth thrash, measured
  flint 19.0 @16 vs 15.0 @32).
- Decode is bandwidth-bound ~26 GB/s: t/s ~= active-bytes-per-token / 26 GB.
- Model file > RAM = ~7 t/s floor (coder IQ1_M finding).
- Zen 4 + AVX-512: ggml VNNI/AVX512 paths; native build picks them. ZenDNN
  (AMD's llama plugin) targets EPYC/NUMA - not applicable to this desktop
  part; re-check if a second CCX path appears.

## Windows 11 tunings (owner-gated, admin)

1. **Defender exclusion** for C:\forge\llama\router\models (25 GB GGUFs get
   scanned at open; exclusion cuts load time). Admin/owner call.
   Add-MpPreference -ExclusionPath "C:\forge\llama\router\models"
2. **Power plan** Ultimate Performance + disable core parking for sustained
   multithreaded decode. powercfg (admin).
3. Pagefile: system-managed is fine at 64 GB RAM; watch commit during loads.
4. Scheduled tasks run at LOGON in the user session (meshd law: no services);
   process priority boost for llama-server is a meshd-phase knob.

## Update discipline (mini-halo law)

- Stage first (no -Swap): the script verifies the staged binaries exist +
  prints --version. Deploy with -Swap: stops lanes, backs up (*pre-stamp.bak),
  copies, restarts the router task, SMOKE = router lists ids; on smoke
  failure restore the .bak files and restart the task.
- GOTCHA (learned 2026-11-12 on first swap): official Windows releases ship
  per-arch variant backends (ggml-cpu-zen4.dll, -haswell.dll, ... + ggml-rpc
  + libomp). The backend registry loads ANY ggml-cpu-*.dll found in the
  directory and PREFERS the best-matching variant over a plain ggml-cpu.dll -
  so an old official build's variants keep loading after a swap, linked
  against the ggml-base.dll we just replaced (mixed-build Frankenstein).
  Fix applied: sweep ALL dated *.dll in the active dir to
  C:\forge\llama\pre-<sha>-oldbuild\ on swap; the active dir must contain
  exactly the staged payload. Verify: loader line must read
  "loaded CPU backend from ...ggml-cpu.dll".
- After any llama.cpp update, RE-MEASURE the laws (portal MODEL_SIGNALS
  pattern): router preset keys (spec-type/draft-mtp on nemotron), swap times,
  flint t/s, load times. Preset keys are build-sensitive; one unrecognized
  key silently rejects presets.ini (portal KB 4.10.2).

## Measured baseline - build 8ca5a80 (0.9.0-dev b50, native AVX-512, live 2026-11-12)

- flint Q4_K_M tg128 @ -t 16: **19.15 t/s** (law held; 15.0 @32 thrash law
  presumed intact); pp512 **164 t/s**.
- Qwen3-4b Q4_K_M tg64 @ -t 16: **19.28 t/s**; pp256 **304 t/s**.
- Router E2E: fingerprint b50-8ca5a80; preset keys all accepted (6 ids,
  zero key rejections in log); on-demand cold load + decode verified.
- Qwen3 thinking models: short max_tokens fills reasoning_content, content
  stays empty (finish_reason=length) - that is the template, not a bug.
- PS 7.7 side-by-side installed (owner): tasks stay pinned powershell.exe
  (5.1); kit .ps1 stay ASCII + 5.1 syntax; pwsh runs them too.
- Bonsai note (owner junk call 2026-11-12): PQ2_0 files are PrismML-fork-only
  (deleted from the library; 7.3 GB). The Bonsai 2 RUNTIME is mainline since
  b11555 (ggml#29600, Oct 2026) - if we ever want the model, take the Q2_0
  packing (prism-ml/Ternary-Bonsai-2-27B-gguf-dev, 7.63 GB) and it runs on a
  current build, no fork. PQ/PTQ types are not mainlined (PTQ1_0 PR#29672
  open; PQ has no PR).

## Version record (CROSS-NODE VERSION SYNC, owner ask 2026-11-16)

Every node records the llama.cpp build it RUNS, next to the install, and its
mesh door reports it:

    C:\forge\llama\version.json   ->  meshd GET /healthz  "llama" field

Written by the builder, from the ref it built: update_llama.ps1 writes it into
the staged release dir (and lands it in the active dir on -Swap);
tools/llama_version.ps1 writes it for installs the updater did not make (hand
build, fresh node). meshd 0.3.1 passes the file through verbatim and reports
NULL when it is absent - no version is claimed that was not measured.
MESHD_LLAMA_VERSION_FILE points a non-Windows node at its own record, so the
portal compares versions across nodes from one field. The record carries
"source": "updater" (built by the updater), "release-match" (active exe matched
by size+mtime against a staged release dir), "exe-mtime" (nothing to match).
Law: never read the version off the BUILD repo - after a staged build the repo
sits at the incoming ref, not at what is running (that probe would lie).

Windows probe law (cured 2026-11-16, cost two rounds): llama tools print
--version to STDERR, and PS 5.1 wraps native stderr into an error record EVEN
UNDER REDIRECTION ("llama-server.exe : version: 1.0.0-dev (...)"), so a
line-anchored regex matches nothing - match unanchored. The probe also needs
the CUDA bin\x64 PATH prefix (see the runtime path law) or it dies 0xc0000135
and yields an empty string.

## Measured - build 9581154 (1.0.0-dev build 50, CUDA sm_75, live 2026-11-16)

Master tip, built MSVC 19.44 + CUDA 13.5, swapped over 8ca5a80 (backups
*.pre-20261116-192835.bak). tools/bench_llama.ps1, -t 16, -pg 512,128, r=2:

- Qwen3-4b Q4_K_M **-ngl 99 (GPU)**: pp512 **4528**, tg128 **135.3 +- 0.7**
  (baseline 8ca5a80: tg64 128, pp256 3723 - GPU lane improved, same home lane).
- flint Q4_K_M **-ngl 0 (CPU)**: pp512 **156.9**, tg128 **18.14 +- 0.08**
  (baseline 19.15 tg128, 164 pp512) - a real ~5 pct CPU-lane regression on this
  host. Watch it: if the next build does not recover it, the CPU decode law
  for this box moves to ~18 t/s and the flint/nemotron preset budget follows.
- Router E2E on the new build (tools/router_smoke.ps1, cold loads, 6 ids
  listed): flint 24.4 s / 20 tok, nemotron 37.3 s / 64 tok. Those are
  load-dominated, NOT throughput. nemotron content came back empty with
  reasoning generated - the thinking-template law, not a bug (see baseline
  note above). Router presets accepted all keys (no presets.ini rejection).
- Lane discipline for smokes: the router is --models-max 1, so a multi-id smoke
  forces swaps (minutes each). tools/router_smoke.ps1 asks for named ids only.

## PENDING (owner gates)

- CUDA toolkit 13.5: DONE 2026-11-12 (winget Nvidia.CUDA + registry
  InstallDir fill + CUDA_PATH_V13_5 process-env import + bin\x64 PATH law).
  CUDA/sm_75 build live; GPU lane measured (see lane map).
- Defender exclusion (owner looking into it 2026-11-12) + power plan
  (admin lines above).
