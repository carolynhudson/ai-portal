# Model signals — how to know what a GGUF can do before loading it

Every decision about a router slot is paid for twice: once to think about it, and
once more if it was wrong, because a wrong guess costs a load (5 min and 77 GiB of
aperture for the Flash-Next file on this box) before the runtime tells you. The
header answers most of those questions in **~105 ms and without touching the
tensor bulk** — measured: `modelindex.scan_cache()` reads all 11 models here in
1.16 s, and the tensor-name walk over a 51.05 GiB shard costs 31 ms.

This file is the rubric: which keys and name patterns carry real information,
what each one unlocks, what it was measured against on this machine, and how it
can lie. The columns land in `model_index` (see §Where the facts land); the
reading of them lives in exactly one function, `modelindex.capabilities()`.

## Reading a file without loading it

A GGUF is a magic `GGUF`, a version, a tensor count, a KV count, then the KV
metadata, then the **tensor-info table** (name, type, shape, offset per tensor),
then the weights. Everything below comes from the first three of those. Two
mechanical rules that decide whether a rule works at all:

* **The array limit.** Arrays longer than `_SKIP_ARRAY_OVER` (64 entries) are not
  read at all — the reader returns the placeholder `"<250002 strings skipped>"`
  to keep header parsing cheap, because a GGUF carries its whole vocabulary in
  the metadata head. That placeholder did two silent things: `len()` reported a
  24-token vocabulary, and no amount of searching a 24-character string finds the
  FIM specials. Every generative model on this box is above the threshold, so a
  capability rule built on the tokenizer was dead on arrival and *looked* like it
  worked, because nothing raised. The fix is not a bigger limit: the two arrays
  the rules need are named in `_KEEP_ARRAYS` (`tokenizer.ggml.tokens`,
  `tokenizer.ggml.added_tokens`) and read whole, then dropped — the vocabulary
  itself is never stored. `test_a_real_sized_vocabulary_is_read_whole_by_the_reader`
  counts 151 936 strings through the real reader, asserting the count rather than
  the absence of a plausible-looking string.
* **Shards: only part 1 has the table.** `X-00001-of-00002.gguf` carries the
  header and the tensor names; `-00002-` reads as `arch=None`. A rule that walks
  tensor names must run on the collapsed set's first part — which is what
  `scan_library` / `scan_cache` do by folding `_SHARD` matches into one model.
  Reading shard 2 alone reports "no draft head" about a model that has one.

## The catalogue

All keys below are looked up under the architecture prefix
(`<arch>.<key>`) except the `tokenizer.*` and `general.*` ones, which are
top-level.

| Signal | Where it is read | What it decides | Measured here |
|---|---|---|---|
| `general.architecture` | header | which prefix every other key lives under; the arch family predicts the KV shape | `qwen35`, `qwen4exp`, `deepseek2`, `llama`, `gemma`, `granite`, `gemma-embedding`, `qwen3` |
| `*.context_length` | header | the context the model was trained for — the portal never overrides it (§4.10.1); it is also the number the fit estimate prices | 262144 (qwen35/qwen4exp), 2048 (embeddinggemma) … 163840 |
| `*.block_count`, `*.full_attention_interval` | header | how many layers hold KV. At interval 4 only every fourth layer does, which is a 4× error avoided | 48–65 blocks, interval 4 on qwen35/qwen4exp; absent (no sliding window) elsewhere |
| `*.attention.head_count`, `head_count_kv`, `*.embedding_length`, `rope_dimension_count` | header | the KV bytes/token arithmetic in `kv_bytes_at()` | drives the "fits here" column |
| `*.expert_count`, `expert_used_count` | header | MoE: bytes in the file ≠ bytes in the compute path | 2 files report `is_moe` (Flash-Next, DeepSeek-Coder-V2-Lite) |
| `ssm.conv_kernel`, `ssm.state_size` | header | the export carries recurrent state — **not** "this is a Mamba model" | True on qwen35/qwen4exp; those files are MoE *and* hybrid, so both columns stay separate |
| `tokenizer.ggml.tokens`, `tokenizer.ggml.added_tokens` | header arrays | fill-in-the-middle: is an autocomplete slot worth starting (below) | 3–4 FIM specials on every generative file, 0 on the embedder |
| `*.nextn_predict_layers` **and** tensor names `blk.N.nextn.*` | header + tensor table | whether the model drafts with its own head: `--spec-type draft-mtp` with **one** file, no draft file, no extra memory | 5 files carry `1` + 4 tensors; the 77 GiB Flash-Next pair carries neither |
| `*.pooling_type` | header | embedder vs reranker — the runtime answers `/rerank` for one and 501s the other | 4 on qwen3-reranker (answered, 52 ms / 2 results), 1 on embeddinggemma (501) |
| `tokenizer.chat_template` and the role markers inside it | header | what the file can actually speak: tool calls, thinking spans | rendered as badges; "no template" is a base model, a different state |
| `clip.has_vision_encoder`, `general.type` on the **sidecar** | the mmproj file | vision input. A property of the export, not the arch, so the scanner (not the reader) sets it | all 8 projectors here declare a vision encoder |
| filename markers `mmproj`, `mtp-`, `draft-`, `dspark-`, `dflash-`, `imatrix-` | filename | not a model: a sidecar. One rule shared with llama.cpp's own `gguf_filename_is_model()` via `NOT_A_MODEL_MARKERS`, **case-sensitive** | one `mtp-Qwen3.8-27B-Q4_0.gguf` (1.28 GiB) sits beside the 27B and is correctly not loadable on its own |
| quant tag in the filename (`Q8_0`, `IQ3_S`, `Q8_K_XL`) | filename | the row's quant column and the size the fit math starts from | — |

### Autocomplete / FIM — the rule, and its one caveat

`/infill` **runs** against any model: it splices `input_prefix` / `input_suffix`
into the prompt whether or not the weights were ever trained for the holes. So
the endpoint answering is not the fact, and a badge built on it would call an
embedder an autocomplete model. What decides is whether the tokenizer has the
specials the weights answer to:

* match a token that contains `fim` (case-insensitive) **and** looks like a
  special (`<...>`). The shape test matters: `fim`, `fimbre`, `fimbul` are
  ordinary BPE pieces and must not make a model look capable.
* accept the **fullwidth bar** spelling too — deepseek2 writes
  `<｜fim▁begin｜>`. Measured: 3 such tokens on `deepseek-coder-6.7b-base`,
  3 on `DeepSeek-Coder-V2-Lite`, and the ASCII bar form
  (less-than, bar, name, bar, greater-than) on every qwen-family file.

The caveat, and it is the reason `capabilities()` never claims more than the
tokenizer supports: **the FIM specials come with the base vocabulary.**
`qwen3-reranker` reports 4 FIM tokens because every qwen3 file has them, and it
is not an autocomplete model — `pooling_type 4` is what says that. So `has_fim`
is necessary, not sufficient; the honest reading is "the tokenizer can express
the holes", and a personality still has to pick a model that was trained to
fill them.

Three states must stay three, all the way to the page:

| stored | badge | meaning |
|---|---|---|
| `has_fim = 1` | `autocomplete` | vocabulary read, specials present |
| `has_fim = 0` | `no FIM` | vocabulary read, specials absent — an autocomplete slot here is wasted aperture |
| `has_fim IS NULL` | `fim?` | vocabulary unreadable — **unaskable**, not absent |

Collapsing the last two is how a working slot gets hidden behind a parse gap.
And note the storage type: SQLite hands back `1`/`0`, so `has_fim is True` was
false for every row fetched from the table — the badge was wrong on the page and
right in the unit test, which is the worst possible split. Truthiness for the
two known states, `is None` for the third.

### Draft heads (MTP) — declared and carried, kept as two columns

`nextn_predict_layers` says the architecture *wants* a draft layer;
`blk.N.nextn.*` in the tensor table says the weights *have* one. They usually
agree and can disagree, and the two columns exist so a disagreement stays
visible:

| declared | tensors | reading |
|---|---|---|
| 1 | 4 | self-drafting. One `--model`, `--spec-type draft-mtp`, no extra memory |
| 1 | 0 | **head missing** — the quantiser dropped it. The server starts and drafts nothing |
| — | 4 | llama.cpp finds the head by name anyway, so it works |
| — | 0 | no speculative decoding available |

Measured on this box, same file (Qwopus 4B Q8_0), ctx 8192, 4 slots (§6.12.11):
decode **41.51 → 95.86 tok/s** (2.31×), tool round-trip 0.531 → 0.439 s, for one
flag. The evidence ships beside the speed — `llamacpp:spec_decode_num_draft_tokens_total`,
`..._accepted_tokens_total`, `..._drafts_total` — and `slot_bench.py` maps them,
so a run claiming a draft speedup has to show the accept rate.

**Acting on it is a separate question.** In b11146 no `--spec-*` key is
preset-eligible: the whole group sits in the argument block presets cannot reach
(`tools/preset_eligibility_probe.py`). The portal writes `presets.ini` and
nothing else, so this is surfaced on the models page and deliberately *not* a
settings control.

### Tool protocol — the template's own special tokens

`tokenizer.chat_template` is stored; `_template_markers()` extracts the protocols
it can actually spell — `chat`, `system`, `tools`, `tool-results` — into
`model_index.template_markers`. Structural match, not substring: every Jinja
template mentions the word `tools` in a loop variable, which says nothing about
what the weights were trained against.

Measured consequence, the whole reason for the column: every cached row whose
markers include `tools` emitted a valid tool call under the bench probe (Qwopus
×2, the 27B, Granite-4.1-3b), while **DeepSeek-Coder-V2-Lite-Instruct — markers
`chat,system` only — emitted nothing at all: 43 tok/s decode,
`tool_call.emitted = false`**. A model can be 6x faster than the 27B and still
be useless as the function slot, and the header said so before a worker started.
`capabilities()` renders it as `tools` / `no tool protocol`.

The runtime answers the same question through `/props → chat_template_caps`
(`supports_parallel_tool_calls`, `supports_object_arguments`) — for a model that
is already loaded. On this box that alternative costs ~50 s and 20-80 GiB.

### What a pooling declaration cancels

`pooling_type` set (1 mean / 4 cls) means the file cannot generate. Its
tokenizer may still declare FIM specials and its template may still carry Qwen's
tool tags — the Qwen3 reranker does both — so `capabilities()` refuses the
generative badges for it. The columns keep the raw facts; the badge keeps the
promise. A reranker badged `autocomplete` is a slot wasted at any speed.

## Cross-file signals: may a projector be borrowed?

A projector is not matched to a model by name. `mtmd.cpp:604` compares exactly one number —
`llama_model_n_embd_inp(text)` against `clip_n_mmproj_embd(mmproj)` — and throws *"mismatch between
text model (n_embd = X) and mmproj (n_embd = Y)"* when they differ. Both numbers are in the headers,
so the verdict exists before a worker starts:

| side | key | this box |
|---|---|---|
| LLM | `<arch>.embedding_length` | 5120 on both Qwen3.8-27B files (unsloth Q8_K_XL, ukisai Swift-1.5 IQ3_S) |
| projector | `clip.vision.projection_dim` | 5120 in unsloth's `mmproj-BF16.gguf`, `projector_type = qwen3vl_merger` |

Three more facts the headers settle, all of which must agree as a *pair*:

* the encoder lives entirely in the projector (`clip.vision.*`: 27 blocks, 1152 hidden, 768 px,
  patch 16, merge 2), so a text-only post-train cannot invalidate it — this is why borrowing works
  at all when the base model is shared (`general.base_model.0.name = Qwen3.8 27B` on both sides);
* the image splice tokens come from the **LLM's tokenizer**, not the projector: `tokenizer.ggml.tokens`
  entries with `token_type = 3` — ``, ``, ``, ``
  are all present in the Swift file (vocab 248 320, identical to unsloth's);
* `tokenizer.chat_template` is the one header a post-train *does* rewrite — 8 952 bytes on Swift
  against 9 993 on unsloth, same base model, same `chat,system,tool-results,tools` markers. Both
  still mention vision; a template that dropped the image placeholder would still pass mtmd's
  numeric gate and then fail at request time.

`tools/mmproj_swap_probe.py` prints the header prediction first and only then starts a worker — the
portal's own build, a spare port, a synthetic PNG with a known colour, and the answer recorded
verbatim. Cost: LLM + projector + graph, ~13 GiB for an IQ3_S-class file.

## What is not a signal

* **The `:TAG` in a cache model id is a file-name artefact, not a quantization.**
  llama.cpp names a cached model by the file name's **last dash-separated token**,
  so `Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf` is
  `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP` (verified against
  `llama-server --cache-list`), and unsloth's `UD-` prefix is dropped by the same
  rule. Ask that repo for `:IQ3_S-mtp` and it reports "not cached" for a file that
  is on disk. The quantization is a separate column (`quant = IQ3` for this file,
  read from the tensor section), and it is the only place to read it from.
* **A tag is a name, and the load path is more forgiving than the listing.**
  `find_best_model()` (download.cpp:683) searches for the tag followed by `.` or `-`
  **anywhere** in the file name, case-insensitively, so `:IQ3_S-MTP` and even `:IQ3_S`
  both land on that one-file repo. That is luck about a single file, not an id: the
  listing, the autoload set, the preset section key and `/models` all use the exact
  tag from `get_gguf_split_info()`. Ask for `:IQ3_S` in a repo that *also* holds a
  real IQ3_S and the loose search can hand back the wrong quant with no complaint.
  `tools/mmproj_swap_probe.py` now resolves exact-tag-first for that reason.


* **Provenance and safety posture are not in the file.** The CIRU "Strix Orca" package is built on
  `orcarouter/Qwen3.8-Flash-Next-Uncensored`, described upstream as refusal-removed with no alignment
  layer added — and nothing in a GGUF of that lineage says so. The only in-file traces worth keeping
  are `general.license.name` (Swift 1.5: `swift-open-license-1.0`, not Apache) and
  `general.base_model.<n>.name`/`.organization`/`.repo_url`. The rest of that story only exists over
  the hub API (card text, base-model chain, sibling repos). A "is this safe for the coder slot" badge
  therefore cannot come from the file, and the index should not pretend it can.

* **Repository and file names.** `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF` is not a
  draft file, and the target repo is not headless: both carry the head, 441
  tensors each. `find_draft` once lower-cased the marker test, matched "MTP"
  inside the *target's* name, and handed the model to itself as its own draft —
  which is the hang the operator hit. The naming rule is
  `NOT_A_MODEL_MARKERS`, case-sensitive, and shared between the portal and the
  bench so there is one opinion.
* **A live endpoint.** `/infill` answering says nothing (above); `/rerank`
  returning 501 is the header's `pooling_type` showing up at runtime.
* **The second shard of a set.** See the shard rule.
* **File size as a parameter count.** No header here states params; the estimate
  is accurate to ~1.2×, which is not a parameter count, so the column stays
  empty rather than inventing a quiet second opinion.
* **`--fit on`'s own idea of the context.** `fit` reduces context to survive; it
  is not a statement about what the model can do (§6.12.7).

## Measured catalogue, 2026-10-02, b11146, this box

Header-only reads. `fim` = FIM specials found, `nn` = `nextn_predict_layers`,
`dt` = `blk.N.nextn.*` tensors, `pool` = `pooling_type`.

| file | arch | GiB | vocab | fim | nn | dt | pool |
|---|---|---:|---:|---:|---:|---:|---:|
| Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002 | qwen4exp | 27.58 | 248320 | 4 | — | 0 | — |
| Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002 | qwen4exp | 51.05 | 248320 | 4 | — | 0 | — |
| Qwopus3.5-4B-coder-Q8_0 | qwen35 | 4.29 | 248320 | 4 | 1 | 4 | — |
| Qwopus3.5-4B-Coder-MTP-Q8_0 | qwen35 | 4.29 | 248320 | 4 | 1 | 4 | — |
| Qwen3.8-27B-UD-Q8_K_XL | qwen35 | 29.30 | 248320 | 4 | 1 | 4 | — |
| mtp-Qwen3.8-27B-Q4_0 *(sidecar, not loadable)* | qwen35 | 1.28 | 248320 | 4 | 1 | 4 | — |
| deepseek-coder-6.7b-base.Q5_K_M | llama | 4.46 | 32256 | 3 | — | 0 | — |
| DeepSeek-Coder-V2-Lite-Instruct-Q8_0 | deepseek2 | 15.56 | 102400 | 3 | — | 0 | — |
| qwen3-reranker-0.6b-q8_0 | qwen3 | 0.60 | 151669 | 4 | — | 0 | **4** |
| embeddinggemma-300M-Q8_0 | gemma-embedding | 0.31 | 262144 | **0** | — | 0 | **1** |
| codegemma-2b-Q5_K_M | gemma | 1.71 | 256000 | 3 | — | 0 | — |
| Granite-4.1-3b-coder.Q8_0 | granite | 3.37 | 100352 | 4 | — | 0 | — |

**The header predicted the runtime, on the same day, without a hint.** The index
said `pooling_type 1` for embeddinggemma and `4` for the reranker; started
separately, `/rerank` returned 501 for the first and 2 results in 48 ms for the
second (`slot-bench-20261002T190327Z`). That is the whole argument for reading
the header: the alternative is starting a worker to find out.

What an autocomplete slot actually costs on this box, at ctx 4096 / 4 slots (slot count is a personality choice — those were the widest-
slot numbers, taken before slot_bench defaulted to one slot)
(`slot-bench-20261002T190111Z`) — the first run where the concurrency probe
measured anything, after the `/infill` payload fix:

| file | decode | infill tok/s (py / c++ / ruby / rust) | 4 concurrent /infill | aperture over the file |
|---|---|---|---|---|
| codegemma-2b Q5_K_M | 99.6 tok/s | 95.3 / 94.3 / 92.2 / 97.0 | 0.13–0.74 s | +2.21 GiB |
| Granite-4.1-3b Q8_0 | 49.8 tok/s | 49.0 / 46.9 / 43.8 / 48.6 | 0.22–1.38 s | +3.92 GiB |

Two things that fall out of it and matter for slot-picking: the operator's usual
77 GiB model (**Flash-Next**) cannot self-draft — no `nextn` declaration, no
head tensors — while the 27B and both Qwopus files can, for free. And of the
twelve files above, exactly one has no FIM specials: the embedder.

### The same signals under load, 2026-10-02 night

| file | role | prefill | decode | infill tok/s | tool call |
|---|---|---|---|---|---|
| Qwen3.8-27B Q8_K_XL, no spec | coder 8192 | 306 / 360 | **7.2** | 6.4–7.1 | yes |
| same + sidecar MTP file | coder 8192 | 292 / 349 | **16.2 / 16.9** | 8.4–19.1 | yes |
| same, own in-file head | coder 8192 | 293 / 346 | **14.8** | 8.2–16.4 | yes |
| DeepSeek-Coder-V2-Lite Q8_0 (MoE 64/6) | coder 8192 | **1394 / 1333** | **43.4 / 26.6** | 39.9–56.2 | **no** |

`slot-bench-20261002T2155*Z` … `2159*Z`. Three decisions fall straight out:

- **A draft head is the biggest single lever on this box**: 7.2 → 14.8 tok/s for
  nothing (the head is inside the target), → 16.2 for a 1.28 GiB sidecar. The
  sidecar's speed edge over the in-file head is inside the noise of a 64-token
  sample; its accept rate is *lower* (0.55 vs 0.62). Use the own head.
- **Autocomplete belongs on a small model regardless of FIM quality**: the 27B
  has FIM specials and delivers 6.4–7.1 tok/s. codegemma-2b delivers 92–103.
- **The fast MoE is not an agent.** 1.4x the aperture headroom, 6x the decode,
  and no tool protocol in its template — the badge that now says so.

Estimator check from the same runs: priced 31.61 GiB against a measured peak of
31.39 with no draft (accurate), but 32.88 priced against 34.89 peak with a draft
file, and the in-file head added ~2.6 GiB over the plain run. **A draft's own KV
pool and compute graph are unpriced in `peak_bytes()`** — still open.

## Residency flags: not model facts, but they set every number measured

`-lm/--load-mode` takes `auto|mmap|none|mlock|mmap+mlock` (auto = mmap unless a backend cannot);
`-lzm/--lazy-mode` takes `auto|on|off` — there is **no `none`** — and `on` requires mmap, applying at
tensors over 4 GiB when `auto`. `-ngl/--gpu-layers` defaults to `auto` on b11146, which is *not* the
same as 99: `auto` may spill layers to CPU when it must. Every decode number in this file was taken
with the tool's defaults; the operator's router runs `-lm mmap -ngl 99 --lazy-mode on` because the
ISTA notes advise it. Those two configurations have never been compared here.

Why it matters: effective weight streaming on this box reads 29.30 GiB ÷ 7.2 tok/s = **4.07 GiB/s**
(27B Q8_K_XL, spec off) and 4.29 GiB ÷ 55.6 tok/s = **5.56 GiB/s** (4B Q8_0, spec off — the 94.6
figure includes its MTP head). A 256-bit bus delivers roughly 250 GB/s, so dense decode here is two
orders of magnitude off the bus and is not HBM-bound. Something about paging, slot count, or partial
offload is setting the ceiling. `slot_bench` now takes `--load-mode`, `--lazy-mode` and
`--gpu-layers`; the value lists are pinned against the installed build's `--help` by test, because a
value the runtime rejects kills the server at startup and the run would report a dead model instead
of a wrong flag.

## What each intended use asks the file to prove

A slot is a *job*, not a model kind, and the jobs compose: an autocomplete slot plus a RAG slot plus a
tool-calling slot for mixed manual/agentic coding; a conversational model plus a speciality sub-agent
plus an embedder for memory; a long-context narrative model for roleplay, creative writing, or a
TTRPG campaign with story memory. The point of the index is to let a person *choose* those
combinations and see whether they fit — not to ship a fixed set of configurations.

| intended use | what the header must show | what measures it here | still unmeasured |
|---|---|---|---|
| autocomplete (`fim`) | FIM specials in `tokenizer.ggml.tokens`; small enough to answer a keystroke | `--role fim`: pp at editor sizes, infill tok/s, burst (multi-slot only) | whether the completion is *correct* |
| coding with a human (`coder`) | `tools` markers, big trained ctx, sane pp | `--role coder`: pp-vs-ctx curve, tool-call probe, draft head | code correctness, ctx-aware recall |
| unattended agent (`agent`) | `tools`, long ctx, head for cheap decode | same, at the ctx the loop will actually reach | long-horizon reliability, drift |
| memory / retrieval (`embed`) | `pooling_type` 1 or 4 (never a generative badge) | `--role embed`: dim, per-query ms, `/rerank` | retrieval quality (nDCG), which needs a corpus |
| roleplay, story, literature | long ctx and the recall that goes with it; **no** pooling; FIM/tool badges irrelevant; licence/safety posture differs from the coding slot | speed, ctx depth, concurrency only | coherence over long context, repetition, style — and today nothing in a GGUF tells you any of it |

Two honest gaps. First, everything measured so far is *capacity and speed*; none of it is *quality*,
which is why the staged accuracy/needle-in-a-haystack harness (§6.12.13) exists as a plan rather than
a page. Second, a role like roleplay has no header signal at all — the file cannot tell you whether a
model is good at long fiction — so the personalities page should price the *fit* (bytes, aperture,
slots, ctx) with confidence and leave the *taste* judgement to the measured run, labelled with the
build and flags that produced it.

## Where the facts land

Stored (the file's own facts, one column each): `vocab_size`, `has_fim`,
`nextn_layers`, `draft_tensors`, `pooling_type`, alongside the older
`ctx_trained`, `layers`, `full_attention_interval`, `is_moe`, `has_ssm_state`,
`has_mmproj`, `sidecar_type`, `template_markers`.

Derived (never stored): `modelindex.capabilities(row)` → list of
`{label, tone, note}`. That function is the only place allowed to combine the
columns; the models page, slot-picking and any future personality decision ask
it, so a rule change is one edit and not four opinions. `tests/test_model_signals.py`
pins each shape, including the two disagreements, and cross-checks the index's
tensor walk against `slot_bench.nextn_tensors()` on every cached model — two
independent implementations that are also each other's only check.

## Re-measuring after a llama.cpp update

1. `tools/preset_eligibility_probe.py` — which keys presets.ini can carry at all
   (this is what settled that `--spec-*` is not settable by the portal).
2. `tools/slot_bench.py --role fim|coder|embed --spec mtp` — the performance
   claims, with the runtime's own accept-rate counters.
3. `tools/spec_preset_probe.py` — what the **running router** would actually pass
   for a model: it reads `GET /models` → `status.args`, which is the resolved
   command line for an *unloaded* model. Answering the preset questions this way
   costs no aperture and does not disturb the router.
3. `HF_HUB_CACHE=~/.cache/huggingface/hub .venv/bin/python -m pytest
   tests/test_model_signals.py -q` — the two cross-checks against real files
   (they skip inside the suite, which points the hub at an empty sandbox).
