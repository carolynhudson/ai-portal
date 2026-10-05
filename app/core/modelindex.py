"""The model index: what each model *is*, and what it *costs* (Phase N1).

Read-only. This module never writes to the model library, never touches
`presets.ini`, and never talks to the router. It reads GGUF headers, does
arithmetic, and stores the result in `portal.db`. Everything that *acts* on these
facts is a later phase (§4.9's N2–N4), so N1 can be wrong without consequence —
which is the only safe order to build this in.

Why a table and not a scan: two live bugs in one week came from the portal not
knowing that a shard is a fraction of a model, or that a hybrid-attention model
pays a quarter of the KV cache the naive formula predicts (PORTAL_PLAN §4.9).
Both are facts about a file that cost a header read to learn, so they get learned
once and stored.

The two rules this module holds itself to:

* **An estimate carries its inputs.** `est_inputs` records which fields produced
  a number, so a bad estimate is diagnosable and re-derivable when the formula
  improves. A bare number cannot be audited.
* **Absent is not zero.** Missing header fields stay None and make the estimate
  None. Filling in a plausible default would produce a confident wrong answer,
  which is the failure mode this whole phase exists to remove.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

from . import db, modelmeta, paths

# llama.cpp's runtime default KV dtype is f16. The naive-formula table in
# PORTAL_PLAN §4.9 was originally computed at f32 and so overstated everything
# by exactly 2x — see the correction there. Kept as a named constant because a
# dtype inside a magic-number formula is invisible.
KV_DTYPES = {"f16": 2, "bf16": 2, "q8_0": 1, "q4_0": 0.5, "f32": 4}


def _whole(x: float | int) -> int | None:
    """A byte count as an int, or None if it is not a finite number.

    `dsize` is fractional for q4_0 KV, so the products here are floats, and
    `int(inf)` raises OverflowError. Guarding on finiteness rather than wrapping
    the conversion means the only value that reaches int() is one it can take —
    so no exception is possible and nothing has to catch one.
    """
    if not math.isfinite(x):
        return None
    return math.floor(x)
DEFAULT_KV_DTYPE = "f16"

# Bytes to keep free for the desktop and the next allocation. Not derived from
# the model: this is a policy number about the machine, deliberately visible.
HEADROOM_BYTES = 4 << 30

#: Files that are never loadable models on their own (PORTAL_PLAN §4.9). The
#: same rule lives in modeladd._AUX_PREFIX_RE — one opinion, reused.
_AUX = ("mmproj", "mtp-", "draft-", "imatrix-", "dspark-", "dflash-")

# Shard suffix: `model-00001-of-00003.gguf`. Part 1 carries the header, so this
# is how a shard set collapses to one loadable model with a summed size.
_SHARD = re.compile(r"[-_](\d+)-of-(\d+)\.gguf$", re.IGNORECASE)


# --------------------------------------------------------------- the memory math
def kv_bytes_at(meta: dict, ctx: int, dtype: str = DEFAULT_KV_DTYPE) -> int | None:
    """KV cache bytes for `ctx` tokens, hybrid-attention aware.

        kv_layers = ceil(block_count / full_attention_interval)
        kv_bytes  = kv_layers * kv_heads * head_dim * 2 (K,V) * dtype * ctx

    `full_attention_interval` is the whole point: at 4, only every fourth layer
    keeps a cache, and counting all layers overstates it 3.6–4.0x on this box's
    own models (§4.9). `head_dim` comes from the rope field when present and
    `embedding_length / head_count` otherwise, which is what llama.cpp computes.

    Two shapes of header are handled, because both exist here:

    * **scalar head counts** — the formula above, with the interval as a divisor;
    * **per-layer head-count arrays** — gemma4 declares `head_count_kv` as one
      entry per layer (16 for full-attention, 4 for sliding-window) and laguna
      declares `head_count` that way. The correct figure is the sum over the
      layers that actually keep a cache, so the interval does not apply on top:
      the array *is* the per-layer statement. Reading an array as a missing scalar
      silently dropped both models from the estimate, which is how this was found.

    Returns None when the header does not say enough to compute it. It never
    guesses: a missing kv-head count stays unknown rather than becoming zero heads,
    because zero heads means "costs nothing" and that is a lie that ships a model
    to an OOM.
    """
    layers = meta.get("block_count")
    emb = meta.get("embedding_length")
    dsize = KV_DTYPES.get(dtype, 2)

    # Per-layer arrays. The cache is the sum over layers of (kv_heads_for_that_
    # layer * min(ctx, that layer's window)), where a sliding-window layer caps at
    # its window instead of at ctx. Both matter, and getting either wrong is a
    # large error rather than a small one: gemma4 marks 50 of 60 layers sliding
    # with a 1024-token window, and summing those kv heads over the full 131072-
    # token context overstated its cache at 137 GiB for a 16 GB model — a number
    # that would have refused a model that runs fine. That is the §4.9 failure
    # mode this index exists to avoid, reproduced in the estimator itself.
    kv_layers_list = meta.get("attention_kv_count_layers")
    if kv_layers_list and layers and emb:
        head = meta.get("rope_dimension_count")
        heads = meta.get("attention_count")
        if not head:
            if not heads and meta.get("attention_count_layers"):
                heads = max(meta["attention_count_layers"])
            if not heads:
                return None
            head = emb // heads
        if not head:
            return None
        windows = meta.get("sliding_window_pattern")
        window = meta.get("sliding_window")
        if windows and window and len(windows) == len(kv_layers_list):
            # Bytes per layer = kv_heads * head_dim * 2 (K,V) * dtype * tokens
            # cached, and tokens cached is min(ctx, window) for a sliding layer
            # and ctx otherwise. Summed over layers — this is a total, not a
            # per-token rate, which is why ctx is inside the sum and not outside:
            # folding it out and dividing back truncates each sliding term to zero
            # and silently deletes 50 of gemma4's 60 layers from its own estimate.
            total = sum(kvh * (min(ctx, window) if flag else ctx)
                        * head * 2 * dsize
                        for kvh, flag in zip(kv_layers_list, windows, strict=False))  # shorter wins
            return _whole(total)

        # No window pattern. An array can still coexist with
        # full_attention_interval, which says only every Nth layer caches at all.
        # Ignoring the interval here overstates the cache by exactly that factor
        # (4x on this box's hybrid models), so sum only the surviving entries.
        interval = meta.get("full_attention_interval") or 1
        if interval > 1:
            cached = [kvh for i, kvh in enumerate(kv_layers_list)
                      if (i + 1) % interval == 0]
            if not cached:
                cached = kv_layers_list[:1]
            return _whole(sum(cached) * head * 2 * dsize * ctx)
        per_token = sum(kv_layers_list) * head * 2 * dsize
        return _whole(per_token * ctx)

    n_kv = meta.get("attention_kv_count")
    n_head = meta.get("attention_count")
    # Mixed shape: laguna declares kv heads as a scalar (8) but query heads as a
    # per-layer array. head_dim is embedding / query-heads, and the head count
    # that matters for that division is the widest one, since a layer with fewer
    # query heads still uses the model's head width.
    if not n_head and meta.get("attention_count_layers"):
        n_head = max(meta["attention_count_layers"])
    if not (layers and n_kv and n_head and emb):
        return None
    head = meta.get("rope_dimension_count") or (emb // n_head)
    if not head:
        return None
    interval = meta.get("full_attention_interval") or 1
    kv_layers = math.ceil(layers / interval) if interval > 1 else layers
    per_token = kv_layers * n_kv * head * 2 * dsize
    return _whole(per_token * ctx)


def est_total(meta: dict, weight_bytes: int, ctx: int,
              dtype: str = DEFAULT_KV_DTYPE) -> dict:
    """What running this model at `ctx` costs, with the inputs it used.

    Weights are the whole model (all shards, plus mmproj/draft sidecars). The
    measured footprint runs ~12 GB above the file size for the 87 GB model
    (§6.12.2), so `runtime_headroom` is added rather than the file size being
    trusted as the answer.
    """
    kv = kv_bytes_at(meta, ctx, dtype)
    inputs = {
        "block_count": meta.get("block_count"),
        "attention_count": meta.get("attention_count"),
        "attention_kv_count": meta.get("attention_kv_count"),
        "embedding_length": meta.get("embedding_length"),
        "full_attention_interval": meta.get("full_attention_interval"),
        "rope_dimension_count": meta.get("rope_dimension_count"),
        # The per-layer shape, when the model has one. A sliding-window model is priced as a sum
        # over layers -- each caching min(ctx, its own window) -- and the scalars above cannot
        # express that. Recording only scalars is what made `est_inputs` claim to hold "the inputs
        # the estimate used" while omitting the inputs that decided the answer for gemma-4 and
        # Laguna: their own estimates could not be reproduced or checked by anything. Absent keys
        # stay absent rather than becoming empty lists, so "this arch has no per-layer shape" and
        # "this row predates us storing it" remain distinguishable.
        "attention_kv_count_layers": meta.get("attention_kv_count_layers"),
        "attention_count_layers": meta.get("attention_count_layers"),
        "sliding_window": meta.get("sliding_window"),
        "sliding_window_pattern": meta.get("sliding_window_pattern"),
        "kv_dtype": dtype,
        "ctx": ctx,
        "weight_bytes": weight_bytes,
    }
    out = {
        "weights": weight_bytes,
        "kv": kv,
        "runtime_headroom": HEADROOM_BYTES,
        "total": None,
        "est_inputs": inputs,
    }
    if kv is not None:
        out["total"] = weight_bytes + kv + HEADROOM_BYTES
    return out


def ctx_that_fits(meta: dict, weight_bytes: int, budget_bytes: int,
                  dtype: str = DEFAULT_KV_DTYPE) -> int:
    """Largest power-of-two context that fits `budget_bytes`, capped at what the
    model was trained for. 0 means "not loaded here at any context" — a fact
    about this box, not about the model.

    The cap matters. The estimate says the 87 GB model on this box could address
    over a million tokens: arithmetically true and useless, because the model
    stops being itself past its trained 262144 and a number that large invites a
    personality to configure a context llama.cpp will not honour. The trained
    ceiling is the honest upper bound of what we would ever ask for.
    """
    avail = budget_bytes - weight_bytes - HEADROOM_BYTES
    if avail <= 0:
        return 0
    trained = modelmeta._as_int(meta.get("context_length")) or 0
    # context_length comes straight out of a GGUF header, so it is attacker-shaped
    # and may be missing, negative, or a float. modelmeta._as_int is the one place
    # that decides what a header number means; reusing it avoids a second, laxer
    # opinion here.
    cap = 1 << (trained.bit_length() - 1) if trained >= 1 else 0

    # Search the power-of-two ladder instead of dividing by a per-token slope.
    #
    # The slope is not constant, and the old `avail // kv_bytes_at(meta, 1)` assumed
    # it was. On a sliding-window model the layers below the window stop growing
    # once ctx passes it, so the marginal cost per token FALLS as ctx rises: gemma-4
    # charges all 60 layers at ctx=1 and only 10 of them at ctx=262144, which made
    # the 1-token slope roughly 2x the true cost at scale. That under-reported the
    # context this box can address — it capped a 31B model at 131072 when it has
    # 84 GiB free and needs 7 GiB for the whole 262144. The error was always in the
    # pessimistic direction, so it looked like caution rather than a bug.
    #
    # The ladder is at most ~21 evaluations of kv_bytes_at, which is a closed-form
    # sum over headers: cheap enough to run per row, and it can never be off by more
    # than one doubling. `cap` bounds the search so a model cannot be offered more
    # context than it was trained for.
    hi = cap if cap >= 1 else 1 << 40
    best = 0
    ctx = 1
    while ctx <= hi:
        kv = kv_bytes_at(meta, ctx, dtype)
        if kv is None:
            return 0        # cannot price it at all: claim nothing
        if kv <= avail:
            best = ctx
        else:
            break           # KV grows with ctx, so nothing larger can fit
        ctx <<= 1
    if best == 0 and cap >= 1:
        # Not even one token at the ladder floor, but the trained cap may itself
        # sit between two rungs; price it directly so a small-context model is not
        # reported as unloadable.
        kv = kv_bytes_at(meta, cap, dtype)
        if kv is not None and kv <= avail:
            return cap
    return best


# ------------------------------------------------------------------- discovery
def _is_aux(name: str) -> bool:
    return name.lower().startswith(_AUX)


def _shard_of(name: str) -> tuple[int, int] | None:
    """(part, total) for a shard filename, else None."""
    m = _SHARD.search(name)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def _header_hash(p: Path) -> str:
    """Cheap identity for incremental rescans: size + mtime + first/last header
    bytes. Not a content hash — the point is to detect a replaced or edited file
    without reading 94 GB, and mtimes alone miss a hardlinked re-add."""
    try:
        st = p.stat()
        stamp = int(st.st_mtime)
    except (OSError, ValueError, OverflowError):
        # ValueError/OverflowError are not paranoia: st_mtime is a float and a
        # corrupt or network filesystem can hand back NaN, which int() rejects.
        return ""
    return f"{st.st_size}:{stamp}"


def scan_library(models_dir: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Walk the model store and return (models, problems).

    One row per *loadable model*, not per file: shards collapse to their part-1
    with `bytes` summed across the set, because that is the number that predicts
    cost. An incomplete shard set is a problem, not a model — it cannot load, and
    today it fails silently.

    `problems` carries what a plain listing cannot say: dangling symlinks (the old
    indexer crashed on these), incomplete shard sets, and files that are not
    models. Never an exception — a scan of ~/ai/models must survive anything it
    finds, since it runs on someone else's directory.
    """
    root = Path(models_dir or paths.LLAMA_MODELS)
    models: list[dict] = []
    problems: list[dict] = []
    if not root.is_dir():
        return models, [{"kind": "missing-dir", "path": str(root)}]

    for entry in sorted(root.iterdir()):
        if entry.is_symlink() and not entry.exists():
            problems.append({"kind": "dangling-symlink", "path": str(entry),
                             "note": "link goes nowhere; the router cannot see it"})
            continue
        if not entry.is_dir():
            continue
        _scan_model_dir(entry, models, problems)
    return models, problems


def _scan_model_dir(entry: Path, models: list[dict], problems: list[dict]) -> None:
    ggufs = sorted(entry.glob("*.gguf"))
    if not ggufs:
        return

    sidecars = [p for p in ggufs if _is_aux(p.name)]
    mains = [p for p in ggufs if not _is_aux(p.name)]
    if not mains:
        problems.append({"kind": "no-loadable-model", "path": str(entry),
                         "note": f"{len(sidecars)} sidecar file(s), no main model"})
        return

    # Collapse shards into whole models keyed by their set.
    sets: dict[str, dict] = {}
    for p in mains:
        sh = _shard_of(p.name)
        if sh:
            base = _SHARD.sub("", p.name)
            key = f"{entry.name}/{base}"
            s = sets.setdefault(key, {"parts": {}, "sidecars": []})
            s["parts"][sh[0]] = p
        else:
            sets[f"{entry.name}/{p.name}"] = {"parts": {1: p}, "sidecars": []}
    for p in sidecars:
        # A sidecar belongs to the model it sits next to; with several mains in
        # one directory the pairing is genuinely ambiguous, which is recorded
        # rather than guessed.
        target = next(iter(sets.values()), None)
        if target:
            target["sidecars"].append(p)

    for key, s in sets.items():
        _model_row(entry.name, "library", entry, key, s["parts"], s["sidecars"],
                   models, problems)


def _model_row(model_id: str, source: str, where: Path, model_key: str,
               parts: dict[int, Path], sidecars: list[Path],
               models: list[dict], problems: list[dict]) -> None:
    """Turn one shard set plus its sidecars into one index row.

    Shared by the library scan and the HF-cache scan deliberately. Two builders
    would drift, and the drift would be invisible from the outside: a cache row
    quietly missing a field the budget math needs is a wrong fit verdict, not a
    crash — the same shape as the imatrix regex bug that a shared column list
    exists to prevent.

    `where` and `model_key` only feed problems, so a complaint still names the
    directory and the model it found, whatever the source.
    """
    have = sorted(parts)
    # Expected count comes from the shard suffix ("…-of-00003"), which is the
    # only statement of completeness available. A file with no suffix is a
    # whole model, so it expects exactly itself.
    sh = next((_shard_of(p.name) for p in parts.values() if _shard_of(p.name)),
              None)
    expected = sh[1] if sh else 1
    missing = [n for n in range(1, expected + 1) if n not in parts]
    if missing:
        problems.append({"kind": "incomplete-shards", "path": str(where),
                         "model": model_key, "have": have, "missing": missing,
                         # The name the router uses: llama.cpp lists a half
                         # downloaded set, so it is worth saying out loud that the
                         # id exists and the load will not.
                         "id": model_id,
                         "note": "shard set is incomplete — cannot be loaded"})
        return
    try:
        head = parts[have[0]]
        total = sum(p.stat().st_size for p in parts.values())
        side_bytes = sum(p.stat().st_size for p in sidecars)
    except OSError as e:
        problems.append({"kind": "unreadable", "path": str(where),
                         "note": f"{type(e).__name__}: {e}"})
        return
    meta = modelmeta.sniff_gguf(head)
    if not meta:
        problems.append({"kind": "not-gguf", "path": str(head),
                         "note": "header did not parse as GGUF"})
        return
    # Sniff the sidecar too, not just its size. The mmproj declares what kind of
        # projector it is (`general.type`, `clip.has_vision_encoder`), so "this model
        # takes image input" is read from a file rather than inferred from a filename
        # prefix or an upstream tag list. Only the first sidecar is opened: it is the one
        # the router would load, and a header read is a few microseconds.
    side_meta = None
    for sp in sidecars:
        side_meta = modelmeta.sniff_gguf(sp)
        if side_meta:
            break
    models.append({
        "id": model_id,
        # 'library' = under ~/ai/models/llama, which the operator owns: taking a
        # model out of it really does take it out of the router's reach.
        # 'cache' = in the Hugging Face cache, where llama.cpp discovers it on its
        # own whatever the portal lists or presets say. The row carries the
        # difference so nothing downstream claims a fence it does not have.
        "source": source,
        "path": str(head),
        "bytes": total,
        "shard_total": len(parts),
        "mmproj_bytes": side_bytes,
        "arch": meta.get("arch"),
        "name": meta.get("name"),
        "quant": _quant_from_name(head.name),
        "ctx_trained": meta.get("context_length"),
        "layers": meta.get("block_count"),
        "kv_heads": meta.get("attention_kv_count"),
        "attention_heads": meta.get("attention_count"),
        "embedding_length": meta.get("embedding_length"),
        "full_attention_interval": meta.get("full_attention_interval"),
        "rope_dimension_count": meta.get("rope_dimension_count"),
        "expert_count": meta.get("expert_count"),
        "expert_used_count": meta.get("expert_used_count"),
        "is_moe": meta.get("is_moe"),
        "has_ssm_state": meta.get("has_ssm_state"),
        # Vision is decided by what sits beside the model, not by the main header:
        # the mmproj is a separate file. `has_mmproj` is the disk's fact — a file is
        # there. `has_vision` prefers the mmproj's OWN declaration
        # (`clip.has_vision_encoder`), which every one of the 8 projectors on this box
        # states, and falls back to the main header's claim. That way "takes image
        # input" is a fact read out of a file rather than inferred from an `mmproj-`
        # filename, and a projector that declared something other than vision would
        # not be silently labelled vision.
        "has_vision": bool(side_meta.get("vision_encoder")) if side_meta
        else bool(meta.get("has_vision")),
        "has_mmproj": bool(side_bytes),
        # What the sidecar says it is. Kept separate from has_vision so a future
        # audio projector can be shown as what it declares instead of being folded
        # into a vision column.
        "sidecar_type": (side_meta or {}).get("declared_type"),
        # What the file says it can speak, read from the chat template it ships
        # with. Stored as a comma-joined string because it is a set of short
        # labels, not a flag: "chat,system,tool-results,tools" is one fact per
        # protocol and a column per protocol would be a schema change every time
        # a new arch invents a new delimiter.
        #
        # Empty string means the template declares none of the tracked protocols —
        # which for a base model is the expected value, not missing data. The
        # template itself being absent is a different state and is carried by
        # has_chat_template so the two are not conflated.
        "has_chat_template": bool(meta.get("has_chat_template")),
        "template_markers": ",".join(meta.get("template_markers") or []),
        # ---- what the weights are able to do, read from the file ----
        # Each of these answers a question a personality has to decide, and each is
        # a fact the file states rather than a verdict about quality. See
        # docs/MODEL_SIGNALS.md for the measurements behind all of them.
        #
        # Autocomplete: /infill answers for any model, because it splices the
        # prefix/suffix into the prompt regardless — so "the endpoint worked" proves
        # nothing about the model. What decides whether an autocomplete slot is worth
        # starting is whether the tokenizer has the holes the weights were trained to
        # fill. None (not False) when the vocabulary could not be read: "unknown" and
        # "no FIM tokens" are different answers and a UI that cannot tell them apart
        # will hide a working slot.
        "vocab_size": meta.get("vocab_size"),
        "has_fim": bool(meta.get("fim_tokens")) if meta.get("vocab_size") else None,
        # Speculative decoding, stated twice by a well-formed file and stored twice
        # because a quantiser can drop a head the header still asks for — then
        # --spec-type draft-mtp starts and buys nothing. Measured worth on this box:
        # 36-41 tok/s to 95-99 tok/s on Qwopus 4B at the same footprint (§6.12.9),
        # and the head costs no extra aperture because it is already in the file.
        "nextn_layers": meta.get("nextn_layers"),
        "draft_tensors": modelmeta.draft_tensors(head),
        # Embedding vs reranking: the runtime refuses /rerank with 501 unless the
        # model was built to rank. This column is the difference — pooling_type 4
        # (qwen3-reranker, answered /rerank in 52 ms) against 1 (embeddinggemma,
        # HTTP 501). It decides whether a RAG slot is one worker or two.
        "pooling_type": meta.get("pooling_type"),
        "is_aux": False,
        "header_hash": _header_hash(head),
        "scanned_at": time.time(),
        "_meta": meta,
    })


# ------------------------------------------------------------- the HF hub cache
#
# llama.cpp finds models in the Hugging Face cache on its own — measured on the
# deployed b11146, a router started with --models-dir pointed somewhere else still
# listed every cache repo as `[cache] org/repo:QUANT`, and a preset section keyed
# by that exact id was accepted and could give it an alias. So the cache is a
# second source the portal has to name the same way the router does, or the
# per-model settings it writes land on a name the router never uses.
#
# The rules below are transcribed from the build in use, not guessed:
#   common/download.cpp:964 common_list_cached_models() — shard part 1 only, tag
#     must be non-empty, id = repo_id + ":" + tag, first one wins.
#   common/download.cpp:661 gguf_filename_is_model() — the markers below are
#     substrings matched case-sensitively, and the name keeps its tag (so
#     `model-Q8_0-mtpr.gguf` is a model: "mtp-" is not in it, while
#     `model-coder-mtp-Q8_0.gguf` is not).
#
# Measured against b11146 over a fixture cache (2026-09-30, ~20 name shapes):
#   * A quant tag in the directory does not count — `Q6_K/model.gguf` is not
#     listed — so the tag has to come from the file name. `Q6_K/model-Q6_K.gguf`
#     is listed, which is the layout `hf download` actually produces.
#   * One shape this box refuses for a reason I could not pin: a lone
#     `sub-eagle3-x/model-Q8_0.gguf` is not listed, yet in a repo holding several
#     candidates the same basename wins the tag. The marker test is therefore
#     transcribed as basename-only (what the source says) and that shape is left
#     out of the pinned table rather than guessed at.
#   common/download.cpp:506 get_gguf_split_info() — the shard suffix and the
#     trailing `[-.]TAG` (upper-cased).
#   common/hf-cache.cpp:374 get_cached_ref() — refs/main wins, else the first
#     readable ref; only that one revision is scanned, so a stale snapshot
#     directory is not a model and must not become one here either.
#
# The case-sensitivity is not a detail. `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF`
# carries `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf`, which llama.cpp lists as a loadable
# model and the portal — lower-casing before the marker test — refused to name. A
# model the router can load and the portal cannot see gets no preset section, so
# every per-model setting for it is inert and the Models page under-reports the
# library. `imatrix` was missing from the list entirely.
_HF_SPLIT = re.compile(r"^(.+)-([0-9]{5})-of-([0-9]{5})$", re.IGNORECASE)
_HF_TAG = re.compile(r"[-.]([A-Za-z0-9_]+)$")
# Public because tools/slot_bench.py has to classify the same files the same way.
# The bench script kept its own list (matched case-insensitively) and so refused
# to bench `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` — a model the router loads — while
# calling `model-Q8_0-eagle3.gguf` a model it should have skipped. One rule, one
# place.
NOT_A_MODEL_MARKERS = ("mmproj", "imatrix", "mtp-", "eagle3-", "dflash-", "dspark-")
_HF_NOT_A_MODEL = NOT_A_MODEL_MARKERS


def _hf_path_probe(rel: Path) -> str:
    """The string llama.cpp runs its "is this a model" test over.

    The basename, case untouched. Transcribed from gguf_filename_is_model(),
    which strips everything before the last '/' — and case is load-bearing, see
    the block comment above."""
    return rel.name
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")


def hub_dir(root: Path | None = None) -> Path:
    """Where the hub cache lives, the way the runtime finds it.

    `HF_HUB_CACHE` is what the huggingface tooling honours, and llama.cpp's own
    `get_cache_path()` consults the same environment. Defaulting to paths.HF_CACHE
    keeps this inside the portal home, so a test sandbox that redirects the home
    redirects the cache too instead of reading the operator's real one.
    """
    if root is not None:
        return Path(root)
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return Path(env)
    return paths.HF_CACHE / "hub"


def _repo_id(dirname: str) -> str | None:
    """`models--org--repo` -> `org/repo`; `models--repo` -> `repo`.

    The same decoding hf-cache.cpp does (folder_name_to_repo). The id matters:
    it is what a preset section has to be called for the router to take it.
    """
    if not dirname.startswith("models--"):
        return None
    rest = dirname[len("models--"):]
    org, _, repo = rest.partition("--")
    return f"{org}/{repo}" if repo else org


def _cached_commit(repo_dir: Path) -> str | None:
    refs = repo_dir / "refs"
    if not refs.is_dir():
        return None
    fallback = None
    for f in sorted(refs.iterdir()):
        if not f.is_file():
            continue
        try:
            commit = f.read_text(errors="replace").split("\n", 1)[0].strip()
        except OSError:
            continue
        if not _COMMIT_RE.match(commit):
            continue
        if f.name == "main":
            return commit
        fallback = fallback or commit
    return fallback


def _hf_split(name: str) -> tuple[str, int, int, str | None]:
    """(base, part, total, TAG) for a cache filename, mirroring get_gguf_split_info.

    TAG is None when the name carries no trailing `[-.]TOKEN` — llama.cpp skips
    such a file as a model, so it is not one here either.
    """
    stem = name[:-5] if name.lower().endswith(".gguf") else name
    part, total = 1, 1
    m = _HF_SPLIT.match(stem)
    if m:
        stem, part, total = m.group(1), int(m.group(2)), int(m.group(3))
    t = _HF_TAG.search(stem)
    return stem, part, total, t.group(1).upper() if t else None


def scan_cache(root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Rows for every model llama.cpp can already see in the hub cache.

    Never an exception, same contract as scan_library — this reads a directory
    the portal does not own, populated by a third-party downloader.
    """
    hub = hub_dir(root)
    if not hub.is_dir():
        return [], [{"kind": "missing-dir", "path": str(hub),
                     "note": "no Hugging Face hub cache here"}]

    models: list[dict] = []
    problems: list[dict] = []
    for repo in sorted(hub.iterdir()):
        if not repo.is_dir():
            continue
        repo_id = _repo_id(repo.name)
        snaps = repo / "snapshots"
        if repo_id is None or not snaps.is_dir():
            continue
        commit = _cached_commit(repo)
        if not commit:
            problems.append({"kind": "no-ref", "path": str(repo),
                             "note": "refs/ is missing or unreadable — neither "
                                     "llama.cpp nor the portal can tell which "
                                     "revision on disk is the current one"})
            continue
        snap = snaps / commit
        if not snap.is_dir():
            problems.append({"kind": "snapshot-missing", "path": str(snap),
                             "model": repo_id,
                             "note": "refs/ names a revision that is not on disk"})
            continue
        _scan_cache_repo(snap, repo_id, models, problems)
    return models, problems


def _quant_bits(tag: str | None) -> int:
    """`Q5_K_M` -> 5, `BF16` -> 16, no digits -> 0. extract_quant_bits()."""
    if not tag:
        return 0
    for i, c in enumerate(tag):
        if c.isdigit():
            try:
                return int(tag[i:])
            except ValueError:
                return 0
    return 0


def _merge_sources(library: tuple[list[dict], list[dict]],
                   cache: tuple[list[dict], list[dict]],
                   ) -> tuple[list[dict], list[dict]]:
    """One index out of two sources, keeping every problem either scan found.

    The library is a set the operator owns; the hub cache is a set llama.cpp
    reads whether the portal looks at it or not (measured: a router started with
    `--models-dir` elsewhere still listed every cache repo by its
    `org/repo:QUANT` id). Indexing both is what lets a per-model section, a budget and
    a fit verdict apply to a model the operator can actually load.

    On a shared id the library entry wins, matching the router's own cascade —
    "if a model exists in both cached and local, local takes precedence"
    (server-models.cpp:702). Worth being straight about the odds: a cache id
    always contains a slash and a library id is a directory name, which cannot,
    so today the two id spaces are disjoint and this branch is defensive. It is
    kept because `INSERT OR REPLACE` makes an unguarded collision a silent
    overwrite, and a source that silently overwrites another model's row is the
    kind of defect that shows up as a wrong fit verdict rather than as an error.
    """
    lib_models, lib_problems = library
    cache_models, cache_problems = cache
    models = list(lib_models)
    problems = list(lib_problems)
    have = {m["id"] for m in models}
    for m in cache_models:
        if m["id"] in have:
            problems.append({"kind": "shadowed-cache-model", "model": m["id"],
                             "path": m["path"],
                             "note": "another source already has this id — the "
                                     "library entry is the one the router loads"})
            continue
        have.add(m["id"])
        models.append(m)
    problems.extend(cache_problems)
    return models, problems


def _best_sibling(sidecars: list[tuple[Path, tuple[str, ...]]],
                  model_dir: tuple[str, ...], tag: str | None) -> Path | None:
    """The projector this model would actually get — find_best_sibling().

    Worth mirroring rather than approximating: an mmproj is real memory and real
    capability, so picking the wrong one (or none) misprices the model and gets
    `has_vision` wrong. The rule is: the sidecar's whole directory must match the
    model's directory prefix, then prefer the deeper match, then a sidecar whose
    path says `-TAG.`, then the closest quant. A repo-root projector therefore
    still attaches to a model filed under a quant subdirectory, at depth 0.
    """
    best: Path | None = None
    best_depth = best_diff = -1
    best_exact = False
    model_bits = _quant_bits(tag)
    for sp, sib_dir in sidecars:
        if model_dir[:len(sib_dir)] != sib_dir:
            continue
        depth = len(sib_dir)
        diff = abs(_quant_bits(_hf_split(sp.name)[3]) - model_bits)
        exact = bool(tag) and f"-{tag.upper()}." in sp.name.upper()
        if (best is None or depth > best_depth
                or (depth == best_depth and exact and not best_exact)
                or (depth == best_depth and exact == best_exact and diff < best_diff)):
            best, best_depth, best_diff, best_exact = sp, depth, diff, exact
    return best


def _scan_cache_repo(snap: Path, repo_id: str, models: list[dict],
                     problems: list[dict]) -> None:
    """One repo revision -> rows keyed `repo_id:TAG`, as the router names them."""
    sets: dict[str, dict] = {}
    sidecars: list[tuple[Path, tuple[str, ...]]] = []
    for entry in sorted(p for p in snap.rglob("*") if p.is_file() or p.is_symlink()):
        if entry.suffix.lower() != ".gguf":
            continue
        base, part, total, tag = _hf_split(entry.name)
        rel = entry.relative_to(snap)
        rel_dir = rel.parent.parts
        probe = _hf_path_probe(rel)
        if "mmproj" in probe:
            # A projector, never a model of its own. Kept with its position relative
            # to the revision, because the pairing below ranks by directory depth —
            # and a sidecar at the revision root has no parts at all, which is what
            # lets a repo-level `mmproj-BF16.gguf` pair with models that live inside
            # per-quant subdirectories.
            sidecars.append((entry, rel_dir))
            continue
        if tag is None or any(s in probe for s in _HF_NOT_A_MODEL):
            continue
        s = sets.setdefault(f"{rel.parent}/{base}", {"parts": {}, "sidecars": [],
                                                    "tag": tag, "id": f"{repo_id}:{tag}"})
        s["parts"][part] = entry

    seen: set[str] = set()
    for key, s in sets.items():
        if s["id"] in seen:
            # llama.cpp keeps the first and drops the rest; recording the shadow is
            # the difference between "the portal knows" and "the portal invented a
            # model that no load can name".
            problems.append({"kind": "duplicate-id", "path": str(snap),
                             "model": s["id"],
                             "note": "another quant in this repo already claims the "
                                     "id — llama.cpp keeps the first, so this one is "
                                     "not loadable under that name"})
            continue
        seen.add(s["id"])
        model_dir = s["parts"][min(s["parts"])].relative_to(snap).parent.parts
        pick = _best_sibling(sidecars, model_dir, s["tag"])
        if pick is not None:
            s["sidecars"].append(pick)
        _model_row(s["id"], "cache", snap, key, s["parts"], s["sidecars"],
                   models, problems)


_QUANT_RE = re.compile(r"(Q\d+(?:_[A-Z]\d*)?|IQ\d+(?:_[A-Z]\d+)?|BF16|F16|F32)",
                       re.IGNORECASE)


def _quant_from_name(name: str) -> str | None:
    """Quant from the filename — the header does not carry it consistently, and
    the export convention (…-Q4_K_M, …IQ4_XS) is what everyone actually reads."""
    m = _QUANT_RE.search(name)
    return m.group(1).upper() if m else None


# ----------------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS model_index (
  id TEXT PRIMARY KEY,
  source TEXT, path TEXT, bytes INTEGER, shard_total INTEGER, mmproj_bytes INTEGER,
  arch TEXT, name TEXT, quant TEXT, ctx_trained INTEGER,
  layers INTEGER, kv_heads INTEGER, attention_heads INTEGER,
  embedding_length INTEGER, full_attention_interval INTEGER,
  rope_dimension_count INTEGER,
  expert_count INTEGER, expert_used_count INTEGER, is_moe INTEGER,
  has_ssm_state INTEGER, has_vision INTEGER, has_mmproj INTEGER,
  sidecar_type TEXT,
  has_chat_template INTEGER, template_markers TEXT,
  vocab_size INTEGER, has_fim INTEGER,
  nextn_layers INTEGER, draft_tensors INTEGER, pooling_type INTEGER,
  est_weights INTEGER, est_kv INTEGER, est_total INTEGER, est_ctx INTEGER,
  est_inputs TEXT,
  scanned_at REAL, header_hash TEXT
);
"""

# Columns written from a scan row, in INSERT order. Derived from one list rather
# than maintained as three (schema, column list, value tuple): the last time that
# was split up, a widening step silently matched nothing and the divergence hid
# inside the abstraction written to prevent it (§ the imatrix regex). Adding a
# fact now means adding it here and to SCHEMA; the row dict is checked against
# this on every write, so a name that exists in one and not the other fails loudly
# at scan time instead of storing NULL forever.
_SCAN_COLUMNS = (
    "id", "path", "bytes", "shard_total", "mmproj_bytes", "arch", "name",
    "quant", "ctx_trained", "layers", "kv_heads", "attention_heads",
    "embedding_length", "full_attention_interval", "rope_dimension_count",
    "expert_count", "expert_used_count", "is_moe", "has_ssm_state",
    "has_vision", "has_mmproj", "scanned_at", "header_hash",
)


def _est_row(m: dict, est: dict, ctx: int) -> dict:
    """A scan row plus its estimate, in column form."""
    row = dict(m)
    row.update({
        "est_weights": est["weights"], "est_kv": est["kv"],
        "est_total": est["total"], "est_ctx": ctx,
        # Stored as JSON text so the inputs survive; parsed back by rows().
        "est_inputs": json.dumps(est["est_inputs"]),
    })
    return row


# Columns written on every scan, in INSERT order. Derived from one list rather
# than maintained as three (schema / column list / value tuple) with nothing
# checking that they agree: the last time that happened, a widening step matched
# nothing, silently did nothing, and the divergence hid inside the abstraction
# that was written to prevent it (the imatrix regex, §4.9 notes).
_WRITE_COLUMNS = (
    "id", "source", "path", "bytes", "shard_total", "mmproj_bytes", "arch", "name",
    "quant", "ctx_trained", "layers", "kv_heads", "attention_heads",
    "embedding_length", "full_attention_interval", "rope_dimension_count",
    "expert_count", "expert_used_count", "is_moe", "has_ssm_state",
    "has_vision", "has_mmproj", "sidecar_type", "has_chat_template", "template_markers",
    "vocab_size", "has_fim", "nextn_layers", "draft_tensors", "pooling_type",
    "est_weights", "est_kv", "est_total", "est_ctx", "est_inputs",
    "scanned_at", "header_hash",
)
_EST_COLS = {"est_weights", "est_kv", "est_total", "est_ctx", "est_inputs"}


def _declared_type(name: str) -> str:
    """The SQL type SCHEMA declares for `name`, read from SCHEMA itself.

    SCHEMA is already the statement of what a column is, so a migration that guesses
    "INTEGER" for everything is a second, weaker opinion that can disagree with it.
    It did: `template_markers` is TEXT in SCHEMA and would have been ALTERed in as
    INTEGER. SQLite stores a string in an INTEGER column without complaining, so a
    fresh install and an upgraded one would have the same column name with different
    sort behaviour and nothing would ever report it.

    Falls back to TEXT for a name SCHEMA does not mention: TEXT can hold any value, so
    an unlisted column cannot silently lose data.
    """
    for line in SCHEMA.splitlines():
        if not re.search(rf"(^|[ ,]){re.escape(name)}\b", line):
            continue
        # SCHEMA puts several columns on one line, so the token after the name is
        # `INTEGER,` — type plus separator. Strip the punctuation before comparing,
        # or every declared type fails the membership test and the whole function
        # silently returns its TEXT fallback for every column, which is exactly the
        # "second opinion that disagrees with SCHEMA" this function exists to avoid.
        after = line.split(name, 1)[1].strip().lstrip(",").split()
        if after:
            declared = after[0].strip(",;() ").upper()
            if declared in ("TEXT", "INTEGER", "REAL", "BLOB"):
                return declared
        return "TEXT"
    return "TEXT"


def _safe_columns(cols: tuple[str, ...]) -> str:
    """Join column names into a SQL fragment, rejecting anything but an identifier.

    This is the only place table/column names are interpolated into SQL, and it
    fails closed: a name that is not a bare identifier raises instead of being
    embedded. Values never come through here — they are always `?` parameters — so
    the dynamic surface of every statement in this module is a list of names this
    function has already vouched for.
    """
    for c in cols:
        if not (isinstance(c, str) and c.isidentifier() and not c.startswith("_")):
            raise RuntimeError(f"model_index: unsafe column name {c!r}")
    # Every name is quoted, so no identifier reaches the statement as bare text. That is
    # the shape the check is asking for, and it is belt-and-braces on top of the allowlist:
    # `isidentifier()` already guarantees a name cannot contain a quote, so quoting cannot
    # change what is accepted. The load-bearing facts are that values are bound parameters
    # (`VALUES (?, ?, ...)`) and a rejected name raises before any SQL exists.
    #
    # `sqlite3.encodes()` would be the stdlib helper for exactly this, but it does not
    # exist on this interpreter (added in 3.15; this box runs 3.14.4) — verified at
    # runtime, not assumed. Quoting by hand is the same output for an allowed name.
    return ", ".join('"' + c + '"' for c in cols)


def _insert_sql() -> str:
    """The INSERT, built once from _WRITE_COLUMNS via the allowlist above."""
    return (f"INSERT OR REPLACE INTO model_index ({_safe_columns(_WRITE_COLUMNS)})"  # noqa: S608
            f" VALUES ({', '.join('?' * len(_WRITE_COLUMNS))})")


def _row_values(row: dict) -> list:
    """Values for _insert_sql, positionally.

    Raises on a column this row cannot fill. That is deliberate: a name present in
    _WRITE_COLUMNS but absent from the scan row would otherwise store NULL forever
    and look like "the model has no value for that", which is the exact silent
    failure this function exists to prevent. Better a loud scan than a quiet NULL.
    """
    missing = [c for c in _WRITE_COLUMNS if c not in row]
    if missing:
        raise KeyError(f"model_index write: row is missing column(s): "
                       f"{', '.join(missing)}")
    return [row[c] for c in _WRITE_COLUMNS]


def init() -> None:
    # rescan() records an audit event through db, so the index cannot be usable
    # without db's own schema in place. Assuming some earlier caller did that is
    # what made two tests here pass only when an unrelated test ran first.
    db.init()
    conn = db._conn()
    conn.executescript(SCHEMA)
    # Column drift: SCHEMA gains a column, _WRITE_COLUMNS does not notice, and the
    # write silently drops the field forever. Fail at startup instead.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    if not cols:  # first init before executescript committed the table
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    # A table already on disk keeps its old shape: CREATE TABLE IF NOT EXISTS does
    # not widen it, so every column added after the first deploy needs an ALTER.
    # Adding it here rather than raising means an upgrade just works; the columns
    # are nullable and the next rescan fills them.
    gap = [c for c in _WRITE_COLUMNS if c not in cols]
    # _safe_columns() validates and joins; splitting its own output back apart
    # would be silly, so validate each name with the same helper and use it raw.
    # A name in _WRITE_COLUMNS that is not an identifier cannot reach ALTER.
    for c in gap:
        _safe_columns((c,))
        # The declared type comes from SCHEMA rather than a hardcoded INTEGER.
        # Assuming INTEGER here would migrate `template_markers` as an integer
        # column while a fresh install creates it as TEXT — two boxes, same column
        # name, different type, and SQLite happily stores a string in an INTEGER
        # column, so the disagreement would only show up as surprising sort order.
        conn.execute(f"ALTER TABLE model_index ADD COLUMN {c} {_declared_type(c)}")
    if gap:
        conn.commit()
    # Anything still missing is a name in _WRITE_COLUMNS that is not a column at
    # all (typo, or not in SCHEMA either) — that is a bug, not an upgrade, and it
    # would otherwise store NULL forever and read as "this model has no value".
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    still = [c for c in _WRITE_COLUMNS if c not in cols]
    if still:
        raise RuntimeError(
            "model_index: _WRITE_COLUMNS names column(s) that are not in the "
            f"table and could not be added: {', '.join(still)}. Add them to "
            "SCHEMA, or remove them from _WRITE_COLUMNS.")
    conn.commit()


def rescan(models_dir: Path | None = None, budget_bytes: int | None = None,
           cache_dir: Path | None = None) -> dict:
    """Rebuild the index. Returns a summary with the problems worth surfacing.

    Incremental in effect if not in mechanism: a rescand is ~34 ms/model of
    header reading, so a full pass over 16 models costs about half a second and
    correctness beats cleverness here. `header_hash` is stored so an N-phase
    follow-up can skip unchanged rows if it ever becomes worth the state.
    """
    init()
    library, cache = scan_library(models_dir), scan_cache(cache_dir)
    models, problems = _merge_sources(library, cache)
    library_n = sum(1 for m in models if m["source"] == "library")

    budget = budget_bytes or _pool_bytes()
    conn = db._conn()
    conn.execute("DELETE FROM model_index")
    # Built once, outside the loop. The string interpolation inside is over
    # _WRITE_COLUMNS — a module-level tuple of literal identifiers, validated by
    # _insert_sql() itself — and every value goes in as a `?` parameter. Hoisting
    # it also makes the shape obvious here: no model data reaches the SQL text.
    insert = _insert_sql()
    for m in models:
        meta = m.pop("_meta")
        est = est_total(meta, m["bytes"], m.get("ctx_trained") or 4096)
        fits = ctx_that_fits(meta, m["bytes"] + m.get("mmproj_bytes", 0), budget)
        conn.execute(insert, _row_values(_est_row(m, est, fits)))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM model_index").fetchone()[0]
    # A cache id that will not load its own file is a problem the index can name
    # without asking the network (modelindex.id_round_trip).
    sib = cache_sibling_map(models)
    for m in models:
        risk = id_round_trip(m, sib.get(str(m["id"]).rsplit(":", 1)[0]))
        if risk:
            problems.append({"kind": risk["kind"], "path": m.get("path") or m["id"],
                             "id": m["id"], "tag": risk["tag"],
                             "would_load": risk.get("would_load", ""),
                             # the difference between "this id loads a different file" and "this id
                             # loads the right file today" is the whole message; an empty would_load
                             # left operators guessing which one they were being warned about
                             "picks_this_file": risk.get("picks_this_file", False)})
    db.event("portal", "model-index",
             f"scanned {n} model(s) — {library_n} from the library, "
             f"{n - library_n} from the hub cache; {len(problems)} problem(s)")
    return {"models": n, "library": library_n, "cache": n - library_n,
            "problems": problems, "budget_bytes": budget}


def _pool_bytes() -> int:
    """Budget in the unit that matters: the GPU aperture on an iGPU, else RAM.

    §6.12.0 — rocm-smi's VRAM is a 512 MiB stub on this box and MemAvailable is
    the wrong gauge, so neither is used here when GTT is readable.

    A probe that cannot answer must not end the scan, so each source is tried in order
    and an unusable one falls through to the next. What was wrong here was *silent*
    fall-through: with no record, "no aperture readable" was indistinguishable from
    "the aperture is tiny", and a bad budget silently poisoned every fit verdict
    (§6.12.0's whole point is that the budget is a measurement with provenance).
    The fallbacks stay; each now records why it was skipped.
    """
    skipped: list[str] = []
    try:
        from .. import metrics
        g = metrics.gpu() or {}
        if g.get("pool_total"):
            return int(g["pool_total"])
        skipped.append("gpu probe returned no pool_total")
    except Exception as e:  # any probe failure means "this source is unusable here"
        skipped.append(f"gpu probe raised {type(e).__name__}")
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        skipped.append("/proc/meminfo had no MemTotal line")
    except OSError as e:
        skipped.append(f"/proc/meminfo unreadable: {e}")
    if skipped:
        # Recorded in the portal event log, which is where the rescan already reports
        # (§6.12.0 treats the budget as a measurement with provenance). A UI-visible
        # "no budget measured" is already handled downstream: fits_query() and
        # personality.validate() both report an unreadable budget as unknown rather
        # than as "does not fit".
        db.event("portal", "model-index",
                 f"memory budget: no readable source ({'; '.join(skipped)}); "
                 f"fit verdicts are reported as unknown")
    return 0


def estimate_ctx(row: dict) -> int:
    """The context `est_kv` was actually priced at -- which is not always `est_ctx`.

    A scan prices the cache at the model's **trained** context and separately records `est_ctx` as
    the largest context that fits the aperture. For a model trained wider than this box can hold
    those are different numbers: `qwen3-emb-8b-Q8_0` stores an estimate made at 40960 beside an
    `est_ctx` of 32768. Treating the stored cache as the price of the smaller context reports a
    trained-context cache as a reduced-context one.
    """
    inputs = row.get("est_inputs") or {}
    return modelmeta._as_int(inputs.get("ctx")) or 0


def _reprice_inputs(row: dict) -> dict | None:
    """The estimator-shaped inputs a stored row can offer for a *different* context.

    Two vocabularies describe the same facts: the scan table stores `layers`, `kv_heads` and
    `attention_heads`, while the estimator reads `block_count`, `attention_kv_count` and
    `attention_count`. `peak_bytes()` used to hand the row itself to `kv_bytes_at()`, so the branch
    written for "price this at another context" looked up `block_count`, found nothing, and returned
    None for every model on this box -- it had never once worked.

    `est_inputs` is the row's own record of what its estimate used, already in the estimator's
    vocabulary, so it is the honest source; the columns only fill fields an older row never
    recorded. A row with no `est_inputs` gets None here and is priced from its stored numbers.
    """
    inputs = row.get("est_inputs")
    if not inputs:
        return None
    out = dict(inputs)
    out.setdefault("block_count", row.get("layers"))
    out.setdefault("attention_kv_count", row.get("kv_heads"))
    out.setdefault("attention_count", row.get("attention_heads"))
    out.setdefault("embedding_length", row.get("embedding_length"))
    out.setdefault("full_attention_interval", row.get("full_attention_interval"))
    out.setdefault("rope_dimension_count", row.get("rope_dimension_count"))
    return out


def repriced_kv(row: dict, ctx: int, dtype: str = DEFAULT_KV_DTYPE) -> int | None:
    """KV cache for a stored row at `ctx`, or None when the row cannot be repriced honestly.

    Repricing is checked before it is trusted: pricing the row's own inputs back at the context
    they were recorded at must reproduce `est_kv`. When it does not, the estimate came from geometry
    the row never recorded, and returning a scalar approximation in its place would be precisely the
    failure this index exists to prevent -- a plausible number that is not the right one. Those rows
    need a rescan, not a guess.
    """
    ctx = modelmeta._as_int(ctx) or 0
    if ctx < 1:
        return None
    inputs = _reprice_inputs(row)
    if inputs is None:
        return None
    at = estimate_ctx(row)
    stored = row.get("est_kv")
    if at and stored is not None and kv_bytes_at(inputs, at, dtype) != stored:
        return None
    return kv_bytes_at(inputs, ctx, dtype)


#: llama.cpp's own defaults for the two features that consume host RAM and never appear in the
#: aperture. Both come from the binary on this box (b10964): its start-up log prints "prompt cache
#: is enabled, size limit: 8192 MiB" and "context checkpoints enabled, max = 32, min spacing =
#: 8192", and common/common.h agrees (cache_ram_mib = 8192, n_ctx_checkpoints = 32 per slot).
DEFAULT_CACHE_RAM_MIB = 8192
DEFAULT_CTX_CHECKPOINTS = 32

#: One context checkpoint, priced from measurement rather than from reading the serializer.
#: Taken from the server's own accounting ("created context checkpoint ... size = N MiB"):
#:
#:   Qwen3.5-0.8B.Q4_K_M       19.266 MiB -- identical at ctx 16384, 32768, 65536 and 131072,
#:                                            and identical at n_tokens 4056 and 4568
#:   Qwythos-9B-...-Q4_K_M      50.251 MiB
#:
#: Constant across context size and across session length, so a checkpoint is a property of the
#: model's cache layout, not of the conversation. That rules out the two formulas an estimator
#: would reach for first -- cost scales with context, or cost scales with session length -- and
#: both would have been wrong. Priced here as the model's KV for CHECKPOINT_TOKEN_BOUND tokens:
#: 29.4 MiB and 131.1 MiB, i.e. 1.5x and 2.6x above what was measured. Above is the only side it
#: is worth being wrong on.
CHECKPOINT_TOKEN_BOUND = 4096


def checkpoint_eligible(row: dict) -> bool:
    """Whether llama.cpp will make context checkpoints for this model at all.

    Not a property of the setting. The server declines it per model (server-context.cpp):

        do_checkpoint = n_ctx_checkpoints > 0 && completion && (
            seq_rm_type == FULL || seq_rm_type == RS || n_swa > 0)

    Measured on this box with `--ctx-checkpoints 32` in effect: two interval-4 archs produced
    checkpoints at every boundary, while Mistral-Code-Architect-7b logged `main/do_checkpoint = no`
    and produced none. That is also why the option is aliased `--swa-checkpoints`.

    The declining condition reads the KV cache's runtime capability, which a GGUF header does not
    state, so this is a layout proxy: hybrid / sliding-window layouts are eligible, plain dense
    layouts are not. When the index records no layout at all the answer is False and the caller is
    told the cost was not priced -- see host_cache_bytes().
    """
    if (modelmeta._as_int(row.get("full_attention_interval")) or 1) > 1:
        return True
    inputs = row.get("est_inputs") or {}
    return bool(modelmeta._as_int(inputs.get("sliding_window")))


def host_cache_bytes(row: dict, checkpoints: int = DEFAULT_CTX_CHECKPOINTS,
                     parallel: int = 1, cache_ram_mib: int = DEFAULT_CACHE_RAM_MIB) -> dict:
    """What this model's server holds in **host RAM** for caching, on top of the aperture cost.

    Two things the aperture estimate does not contain, both on by default:

    - the prompt cache, capped by `cache-ram` at 8192 MiB unless changed; `0` disables it, and
      `-1` means no limit -- which cannot be priced, so it returns None rather than a number.
    - context checkpoints: `checkpoints` copies per slot, `parallel` slots, each a host-RAM
      snapshot of KV state. GTT did not move at all across checkpoint-on and checkpoint-off runs
      on this box while RSS rose by exactly the logged checkpoint total, so this is host RAM and
      must not be checked against the aperture alone.

    Both are returned so a caller can show them separately; `total_bytes` is None when any part
    cannot be priced, because a partial total is how a capacity check turns into a guess.
    """
    notes: list[str] = []

    ram_mib = modelmeta._as_int(cache_ram_mib)
    if ram_mib is None:
        cache_bytes = None
        notes.append("cache-ram is not a number; prompt cache cost unknown")
    elif ram_mib < 0:
        cache_bytes = None
        notes.append("cache-ram = -1 means no limit: an unbounded cache cannot be verified")
    elif ram_mib == 0:
        cache_bytes = 0
        notes.append("cache-ram = 0 disables the prompt cache")
    else:
        cache_bytes = ram_mib * 1024 * 1024

    n_cp = modelmeta._as_int(checkpoints) or 0
    # `parallel = -1` is llama.cpp's "auto", not a negative count. Clamped up rather than down:
    # auto resolves to several slots on this box, and pricing a per-slot cost at one slot each
    # would understate it.
    n_par = max(1, modelmeta._as_int(parallel) or 1)
    if n_cp <= 0:
        cp_bytes = 0
        notes.append("ctx-checkpoints = 0: no checkpoint copies")
    elif not checkpoint_eligible(row):
        cp_bytes = 0
        notes.append(
            "no checkpoint cost priced: the index records no sliding-window or hybrid layout for "
            "this model, and llama.cpp declines checkpoints for those -- but the decline depends on "
            "the KV cache's runtime capability, which a GGUF header does not state")
    else:
        each = repriced_kv(row, CHECKPOINT_TOKEN_BOUND)
        if each is None:
            cp_bytes = None
            notes.append("cannot price one checkpoint: this row cannot be repriced")
        else:
            cp_bytes = each * n_cp * n_par

    total = None if (cache_bytes is None or cp_bytes is None) else cache_bytes + cp_bytes
    return {
        "prompt_cache_bytes": cache_bytes,
        "checkpoint_bytes": cp_bytes,
        "total_bytes": total,
        "checkpoints": n_cp,
        "parallel": n_par,
        "checkpoint_eligible": checkpoint_eligible(row),
        "notes": notes,
    }


def ram_total_bytes() -> int | None:
    """Physical RAM, which is the pool the GTT aperture is carved out of.

    On this box: 122.8 GiB of RAM and a 108.00 GiB GTT aperture. A model that fits the aperture
    exactly leaves nothing for the prompt cache and checkpoints, which live in the same physical
    RAM -- the shape of "it should fit but the kernel killed it".
    """
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def peak_bytes(row: dict, ctx: int | None = None,
               dtype: str = DEFAULT_KV_DTYPE) -> int | None:
    """What this model costs resident at `ctx`, including headroom — the number a
    loader needs before it decides a model is safe to bring up.

    `est_total` is weights + cache at the context the estimate chose. That is fine
    for a table and wrong for a scheduler, which asks a different question: *if I
    load this now, at context X, how much of the aperture goes away*. This answers
    that, and returns None rather than a guess when the cache cannot be priced — a
    scheduler that receives None must refuse, one that receives 0 will not.

    Headroom is included because the aperture is not the only consumer of RAM and a
    plan that accounts for exactly the model is a plan that swaps. It is the same
    HEADROOM_BYTES ctx_that_fits uses, so "peak fits in budget" and "ctx_that_fits
    said >= ctx" cannot disagree.
    """
    # Header-derived numbers reach here as-is, and a GGUF that declared an
    # absurd block_count or embedding_length can carry inf/NaN through the estimate.
    # `_whole` is the estimator's own opinion about turning a float cost into bytes;
    # using it means an unpriceable input becomes None ("cannot plan") rather than
    # an OverflowError escaping into a page render or a scheduler's arithmetic.
    weights = _whole(row.get("est_weights") or 0)
    if not weights:
        return None
    # `modelmeta._as_int` is the one place that decides what a number from a header
    # or a caller means: it returns None for garbage instead of raising, so a str or
    # an object passed as ctx becomes "cannot plan" rather than a TypeError from the
    # comparison below escaping into a page render.
    ctx = modelmeta._as_int(row.get("est_ctx") if ctx is None else ctx) or 0
    if ctx < 1:
        # No context to price at. Weights plus headroom is a floor, not a peak, and
        # the difference is the cache — the part that swaps a box. Report that we
        # cannot plan rather than hand a scheduler a number it will treat as a peak.
        return None
    # Reusing the stored cache is only correct when that cache *is* the price of this context.
    # estimate_ctx() says where the stored number was made, and it is often the trained context
    # rather than est_ctx (see its docstring). A row with no recorded inputs -- a legacy row, or a
    # hand-built one in a test -- has nothing to check against and is priced from its stored numbers.
    if ctx == row.get("est_ctx") and estimate_ctx(row) in (0, ctx):
        kv = row.get("est_kv")
    else:
        kv = repriced_kv(row, ctx, dtype)
    if kv is None:
        return None
    kv_whole = _whole(kv)
    if kv_whole is None:
        return None
    return weights + kv_whole + HEADROOM_BYTES


def headroom_after(row: dict, budget_bytes: int | None = None,
                   ctx: int | None = None) -> int | None:
    """What the aperture still has for the *next* model if this one is loaded.

    Router planning is a sequence of these, not one decision: `--models-max 1` today
    but a policy that keeps two warm needs to know what a load leaves behind. None
    propagates — an unpriced model has unknown consequences, which is not the same
    as no consequences.
    """
    budget = budget_bytes if budget_bytes is not None else _pool_bytes()
    if not budget:
        return None
    peak = peak_bytes(row, ctx)
    if peak is None:
        return None
    return budget - peak


AUTO_PARALLEL_SLOTS = 4      # server.cpp:161-166 -- what "auto" resolves to, measured here


def free_pool_bytes() -> tuple[int, str]:
    """What the aperture has left right now, and how that was measured.

    fit measures free device memory, not the size of the aperture, so a prediction that uses the
    total answers a question the runtime is not asking. Free is the honest default; when the used
    gauge is unreadable the total is used and said out loud, because a silent optimism here is
    the same class of error as a silent 64 GiB budget (§6.12.0).
    """
    total = _pool_bytes()
    try:
        from .. import metrics
        g = metrics.gpu() or {}
        used = int(g.get("pool_used") or 0)
        total = int(g.get("pool_total") or total)
        if total and used:
            return max(0, total - used), f"free aperture now: {_gi(max(0, total - used))} of {_gi(total)}"
        return total, f"aperture total: {_gi(total)} (used gauge unreadable)"
    except Exception:  # noqa: BLE001 -- a probe failure is a weaker budget, not a failure
        return total, f"aperture total: {_gi(total)} (gpu probe unusable)"


def _gi(x: int) -> str:
    """Bytes as a person would read them. Local, and only ever given a real number."""
    return f"{(x or 0) / (1 << 30):.1f} GiB"


def context_window(row: dict, settings: dict | None = None,
                   budget_bytes: int | None = None) -> dict:
    """The context a client will be given for this model, and **who decided it**.

    The number a client reads is `n_ctx_slot()` -- `min(llama_n_ctx_seq, --kv-unified-per-slot,
    n_ctx_train)` (server-context.cpp:4246-4254), reported by /props, /slots and /v1/models. It is
    not a setting; it is the result of up to three settings plus a chooser, which is why "the
    router says this model has half the context it used to report" was hard to trace
    (docs/PORTAL_PLAN.md §6.12.20, which carries the measurement table this encodes).

    * slots **auto** -> unified KV: one pool, and every slot can reach all of it.
    * slots **named** -> KV is not unified: `n_ctx_seq = pool / slots` (llama-context.cpp:295), so
      naming slots divides the number every client reads.
    * `ctx-size` **unset** -> llama.cpp sizes the pool for the trained context and `--fit` shrinks
      it when the aperture cannot pay, logging at trace level only. The silent case.
    * `ctx-size` **pinned** -> fit is locked out and the written number is the ceiling.

    `window` is None when the model cannot be priced. "Cannot predict" is reported as such; an
    optimistic guess about a client's context window is the thing this function exists to prevent.
    """
    s = settings or {}
    trained = modelmeta._as_int(row.get("ctx_trained")) or 0
    slots_raw = modelmeta._as_int(s.get("parallel")) or 0
    slots_named = slots_raw > 0
    slots = slots_raw if slots_named else AUTO_PARALLEL_SLOTS
    per_slot = modelmeta._as_int(s.get("kv-unified-per-slot")) or 0
    ctx_set = modelmeta._as_int(s.get("ctx-size")) or 0

    kvu = str(s.get("kv-unified") or "auto").lower()
    unified = True if kvu == "on" else False if kvu == "off" else (not slots_named)

    if ctx_set:
        pool_want = ctx_set
    elif per_slot:
        pool_want = slots * per_slot
    else:
        pool_want = trained if unified else trained * slots

    out: dict = {
        "trained": trained, "slots": slots, "slots_named": slots_named, "kv_unified": unified,
        "ctx_size": ctx_set or None, "kv_unified_per_slot": per_slot or None,
        "pool_want": pool_want, "pool": None, "window": None,
        "reduced": None, "by": "unpriced", "notes": [], "budget_bytes": None, "budget_note": "",
    }
    if pool_want < 1:
        out["notes"].append("no trained context in the header and no ctx-size written")
        return out

    budget = modelmeta._as_int(budget_bytes) or 0
    if budget:
        out["budget_note"] = f"the budget you passed: {_gi(budget)}"
    else:
        budget, out["budget_note"] = free_pool_bytes()
    peak = peak_bytes(row, pool_want)
    pool = pool_want
    shrunk = False
    if peak is None:
        out["notes"].append("cannot price this model's cache, so its window is not predictable")
        out["pool"] = pool
        return out
    if budget and peak > budget:
        pool = 0
        c = 1
        while c <= pool_want:
            p = peak_bytes(row, c)
            if p is None:
                break
            if p <= budget:
                pool = c
            else:
                break
            c <<= 1
        shrunk = pool < pool_want
        priced = peak_bytes(row, pool) if pool else 0
        out["notes"].append(
            f"{slots} slot(s) at the trained context cost {_gi(peak)} against {_gi(budget)} of "
            f"aperture -- fit sizes the pool to {_gi(priced or 0)} and logs the reduction at "
            f"trace level only, so nothing in the router's log says the context changed")

    if pool < 1:
        # Nothing fits, not even one token. Say that, rather than take a min over an empty set.
        out["pool"] = 0
        out["window"] = None
        out["reduced"] = True
        out["by"] = ("does not fit this box at any context -- the weights alone are over the "
                     "budget, so ctx-size is not the decision to make here")
        out["notes"].append(
            f"weights + the smallest cache cost more than {_gi(budget)} "
            f"({out['budget_note']})")
        return out

    seq = pool if unified else pool // slots
    window = min(v for v in (seq, per_slot or seq, trained or seq) if v > 0)
    out["pool"] = pool or None
    out["window"] = window or None
    out["reduced"] = bool(trained) and window < trained

    if ctx_set:
        out["by"] = "ctx-size (written): fit is locked out, so this is the ceiling"
    elif per_slot:
        out["by"] = "kv-unified-per-slot (written): the window is that number"
    elif shrunk:
        out["by"] = ("fit, because ctx-size is unset and the trained context does not fit -- "
                     "writing ctx-size takes the decision back")
    elif out["reduced"]:
        out["by"] = "slots divide the pool: parallel is written, so KV is not unified"
    else:
        out["by"] = "the model's trained context, afforded"

    if slots_named and not unified and ctx_set:
        out["notes"].append(
            f"ctx-size {ctx_set} across {slots} named slots is a {window}-token window per "
            "client -- naming slots turns the unified pool off and divides it "
            "(llama-context.cpp:295)")
    if out["reduced"] and trained:
        out["notes"].append(f"window {window} is below the model's trained {trained}")
    return out


def router_key(name: str) -> str:
    """Key a library directory and a router model id agree on.

    The router registers some models under a HuggingFace-style alias — `unsloth/
    Qwen3.8-27B-GGUF:Q8_K_XL` — while the library knows the directory name. Matching
    on the raw strings left that model looking unloaded while it was serving traffic,
    which is the failure mode a "live" indicator must never have: a wrong "not
    running" invites the user to load a second copy of a model that is already up.

    Lowercase, strip namespace and `:tag`, drop quant and separator noise. Two
    different quants of one architecture stay distinct (they are different files);
    one file under two spellings collapses.
    """
    s = (name or "").strip().lower()
    s = s.rsplit("/", 1)[-1]        # drop `org/`
    s = s.split(":", 1)[0]          # drop `:quant`
    s = re.sub(r"[ ._-]+", "", s)
    return s


def live_keys(live: list[dict]) -> dict[str, dict]:
    """Router models keyed for lookup by library row.

    Returns a map rather than a set so the caller can show *what the router calls it*
    when that differs — a model reachable under an alias is a fact worth surfacing,
    not something to hide behind an identical-looking name.
    """
    out: dict[str, dict] = {}
    for m in live or []:
        rid = m.get("id") or ""
        if not rid or rid.startswith("(router unreachable"):
            continue
        out.setdefault(router_key(rid), {"id": rid, "created": m.get("created")})
    return out


def verdict(row: dict, budget_bytes: int | None = None) -> str:
    """Can this model run here: 'yes' | 'reduced' | 'never' | 'unknown'.

    Four states, not three, because two of them must not be merged:

      yes      weights + KV at full trained context fit the aperture
      reduced  it fits, but not at its trained context — `est_ctx` is the cap
      never    the weights alone do not fit, whatever the context
      unknown  the header did not say enough to compute an estimate

    `unknown` is separate from `never` on purpose. "I could not tell" and "this
    cannot work" produce the same blank cell and opposite actions; collapsing them
    is how a reader learns to distrust the whole column. It is also the case that
    a model whose estimate is missing must never be auto-selected (§ fits_query).

    Note this is a statement about the *aperture*, not free RAM: on this box GTT
    is the pool and MemAvailable is a different, misleading number (§6.12.0).
    """
    total = row.get("est_total")
    if total is None:
        return "unknown"
    budget = budget_bytes if budget_bytes is not None else _pool_bytes()
    if not budget:
        return "unknown"          # no readable pool: we cannot make a claim
    if total <= budget:
        # Compare against the same power-of-two ceiling ctx_that_fits() reports,
        # not the raw trained length. A model trained at 40960 tokens can never
        # show est_ctx >= 40960 under a power-of-two cap — even with unlimited
        # memory — so the raw comparison called it "reduced" on this box's own
        # embedding model, advising a smaller context than the hardware supports.
        # `total <= budget` already means it fits at its trained context; this
        # only decides whether to say so.
        trained = row.get("ctx_trained") or 0
        ceiling = 1 << (trained.bit_length() - 1) if trained >= 1 else 0
        return "yes" if (row.get("est_ctx") or 0) >= ceiling else "reduced"
    # Over budget at full context — does it fit at any context at all?
    weights = row.get("est_weights")
    if weights is None:
        return "unknown"
    return "reduced" if weights <= budget else "never"


def rows() -> list[dict]:
    init()
    conn = db._conn()
    conn.row_factory = db.sqlite3.Row
    out = []
    for r in conn.execute("SELECT * FROM model_index ORDER BY bytes DESC"):
        d = dict(r)
        if d.get("est_inputs"):
            # A row written by an older build, or a hand-edited index, can hold anything
            # in this TEXT column. Keep the raw value rather than dropping the row: the
            # rest of the row is still true, and swallowing the parse error silently
            # made "we could not read the inputs" indistinguishable from "there were
            # none".
            try:
                d["est_inputs"] = json.loads(d["est_inputs"])
            except (TypeError, ValueError) as e:
                d["est_inputs_error"] = f"{type(e).__name__}: {e}"
                db.event("portal", "model-index",
                         f"{d.get('id')}: est_inputs was not valid JSON "
                         f"({type(e).__name__}); kept raw")
        out.append(d)
    return out


def fits_query(need_bytes: int, limit: int = 3) -> list[dict]:
    """The smoke-test question, as a query: models that fit in `need_bytes`.

    Replaces the size heuristic that once picked shard 1 of a 94 GB model
    (§6.11). Ordering is by whole-model bytes ascending, so the cheapest safe
    model wins — and only models whose header is readable appear at all.
    """
    init()
    conn = db._conn()
    conn.row_factory = db.sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM model_index WHERE est_total IS NOT NULL AND est_total <= ?"
        " ORDER BY bytes ASC LIMIT ?", (need_bytes, limit))]


# --------------------------------------------------------------- capabilities
POOLING_RERANK = 4        # measured: qwen3-reranker answered /rerank
POOLING_MEAN = 1          # measured: embeddinggemma returned 501 for /rerank


def capabilities(row: dict, siblings: list[str] | None = None) -> list[dict]:
    """What this file can be used for, and how sure we are.

    Derived, never stored: the stored columns are the header's own facts
    (`has_fim`, `draft_tensors`, `nextn_layers`, `pooling_type`), and this is the
    one place allowed to read them together. Personality code, the models page
    and any future slot-picking all ask here instead of re-deriving, so a rule
    change is one edit and not four opinions.

    Each entry is {"label", "tone", "note"}; `tone` is "ok" (the file can do it),
    "warn" (the header and the weights disagree, or trying it wastes a slot) or
    "mut" (checked and absent / not answerable). Three distinct results have to
    stay distinct: `/infill` answers for *any* model, so a badge built on the
    endpoint would call an embedder an autocomplete model. The tokenizer is the
    only honest signal, and `has_fim IS NULL` means the vocabulary was
    unreadable — which is not the same as having no FIM specials.
    """
    out: list[dict] = []

    # Read as three states, and tolerant of the storage type: SQLite hands back 1
    # and 0 for a BOOLEAN column, so `has_fim is True` was false for every row
    # that came out of the table — the badge silently became "fim?" on a model
    # whose FIM specials were known. Truthiness for the two known states,
    # `is None` for the third.
    # Three states, and tolerant of the storage type: SQLite hands back 1 and 0
    # for the BOOLEAN column, so testing `has_fim is True` was false for every row
    # read out of the table, and a model with known FIM specials got the "unknown"
    # badge. Truthiness for the two known states, `is None` for the third.
    # A pooling model cannot generate at all, so its tokenizer's FIM specials and
    # its inherited chat template say nothing it could act on — the Qwen3 reranker
    # declares both and would be badged as an autocomplete model that can take
    # tools. The pooling badge below is the whole story for that file.
    generative = not row.get("pooling_type")
    has_fim = row.get("has_fim")
    if not generative:
        pass
    elif has_fim is None:
        out.append({"label": "fim?", "tone": "mut",
                    "note": "vocabulary could not be read — autocomplete capability "
                            "is unaskable here, not absent"})
    elif has_fim:
        out.append({"label": "autocomplete", "tone": "ok",
                    "note": "tokenizer declares fill-in-the-middle specials — the "
                            "weights were trained for the holes /infill splices in"})
    else:
        out.append({"label": "no FIM", "tone": "mut",
                    "note": "vocabulary carries no FIM specials — /infill still "
                            "answers, but nothing was trained for it, so an "
                            "autocomplete slot here is wasted memory"})

    # Whether the chat template spells out a tool protocol at all. Measured on
    # this box: every cached row whose markers contain `tools` emitted a valid
    # tool call under the bench probe, and the one row whose template carries
    # only chat+system — DeepSeek-Coder-V2-Lite at 43 tok/s decode — emitted
    # nothing at all. Fast is not usable for agents, and the header says so
    # before a worker is started. (The runtime agrees through /props
    # chat_template_caps, but only for a model that is already loaded.)
    markers = set((row.get("template_markers") or "").split(",")) - {""}
    if generative and row.get("has_chat_template") and markers:
        if "tools" in markers:
            out.append({"label": "tools", "tone": "ok",
                        "note": "the chat template emits tool tags, so the weights "
                                "were trained against a tool protocol: this is the "
                                "file to put in the function/agent slot"})
        else:
            out.append({"label": "no tool protocol", "tone": "mutewarn",
                        "note": "template carries chat/system but no tool tags — a "
                                "tool request comes back as prose. Measured on a "
                                "cached row: 43 tok/s and no call at all"})

    layers = row.get("nextn_layers") if generative else None
    tensors = row.get("draft_tensors") if generative else None
    if layers and tensors:
        out.append({"label": f"draft x{layers}", "tone": "ok",
                    "note": f"carries its own speculative-decoding head "
                            f"({tensors} blk.N.nextn.* tensors) — --spec-type "
                            f"draft-mtp takes this one file and costs no extra "
                            f"memory; measured 2.3x decode (§6.12.11)"})
    elif layers and not tensors:
        out.append({"label": "head missing", "tone": "warn",
                    "note": f"header asks for {layers} draft layer(s) but the tensor "
                            f"table carries none — the quantiser dropped it, and "
                            f"--spec-type draft-mtp will start and draft nothing"})
    elif tensors and not layers:
        out.append({"label": f"draft x{tensors}t", "tone": "ok",
                    "note": "tensor names carry a draft head that the metadata does "
                            "not declare — llama.cpp finds it by name, so it works"})

    pooling = row.get("pooling_type")
    if pooling == POOLING_RERANK:
        out.append({"label": "reranker", "tone": "ok",
                    "note": "pooling_type 4 — /rerank works (measured 52 ms for 2 "
                            "results); an embedder in this slot returns 501"})
    elif pooling == POOLING_MEAN:
        out.append({"label": "embedder", "tone": "info",
                    "note": "pooling_type 1 — embedding model; /rerank is refused "
                            "with 501, so a RAG slot wanting a ranker needs a "
                            "second worker"})
    elif pooling is not None:
        out.append({"label": f"pool {pooling}", "tone": "mut",
                    "note": "unrecognised pooling_type — the runtime's own enum "
                            "value is unknown to the portal"})

    # A hub-cache row is addressed by an id the runtime invented from a file name,
    # and that id is re-resolved by a different rule at load time. Only worth
    # saying for cache rows — a library row is addressed by its directory.
    if row.get("source") == "cache" and row.get("id"):
        risk = id_round_trip(row, siblings)
        if risk and risk["kind"] == "id-resolves-elsewhere":
            out.append({"label": "id loads another file", "tone": "warn",
                        "note": f"`:{risk['tag']}` resolves to {risk['would_load']}, "
                                "not this file — name the path, or the personality "
                                "gets weights it did not choose"})
        elif risk:
            out.append({"label": "id not load-safe", "tone": "warn",
                        "note": f"the runtime lists this file as `:{risk['tag']}`, but "
                                "loads `repo:TAG` by substring match, and that tag is "
                                "not a quantization — a sibling file in the repo can "
                                "win the load (measured: `:MTP` pulled a 12 GiB IQ2_S "
                                "while IQ3_S sat cached). Load by path, or run the "
                                "router --offline so a miss is an error, not a download"})
    return out


# ------------------------------------------------- id round-trip: does the id load the file?
#
# A cache id is a *name*, not a handle. llama.cpp prints one id when listing
# (`llama-server --cache-list`, and our own scan_cache follows the same rule) and
# resolves it differently when loading: find_best_model in common/download.cpp
# takes the tag after the colon, builds the regex `TAG[.-]` case-insensitively,
# and returns the FIRST file in the repository's listing that matches and is not a
# sidecar name. Measured on this box (2026-10-03): loading
# `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP` — an id the runtime itself
# printed for the cached IQ3_S file — started a download of
# `Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ2_S-mtp.gguf`, a different quantization of the
# same repo, because IQ2_S-mtp sorts first and both match /MTP[.-]/. A personality
# naming that model would quietly trade its weights for a smaller quant and pull
# 12 GiB to get them. --offline turns the surprise into an error; this predicts it.
_LOADER_AUX = ("mmproj", "imatrix", "mtp-", "eagle3-", "dflash-", "dspark-")
_SHARD_OF = re.compile(r"-(\d+)-of-(\d+)\.gguf$", re.I)


def gguf_is_model_name(path: str) -> bool:
    """llama.cpp gguf_filename_is_model: a .gguf that is not a sidecar name.

    The sidecar test is a substring test for `mtp-`, `mmproj` and friends — with
    the dash. A file named `...-IQ3_S-mtp.gguf` (mtp as a SUFFIX) therefore counts
    as a model to the runtime, which is how a draft-head-bearing file ends up with
    its own cache id in the first place.
    """
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith(".gguf") and not any(a in name for a in _LOADER_AUX)


def loader_pick(filenames: list[str], tag: str) -> str | None:
    """Which file `repo:TAG` actually resolves to, ported from find_best_model."""
    for t in ([tag] if tag else ["Q4_K_M", "Q8_0"]):
        pat = re.compile(re.escape(t) + r"[.-]", re.I)
        for f in filenames:
            if not gguf_is_model_name(f) or not pat.search(f):
                continue
            shard = _SHARD_OF.search(f)
            if shard and int(shard.group(1)) != 1:
                continue  # only shard 1 is a loadable entry point
            return f
    return None


def id_round_trip(row: dict, siblings: list[str] | None = None) -> dict | None:
    """None if `row["id"]` names this file; otherwise what is wrong with it.

    Two levels of certainty, kept separate on purpose. If the runtime's own rule
    picks a *different* file from the ones we can see, that is a fact. If the tag
    is not a quantization at all (`:MTP`, `:Chat` — no digits, so llama.cpp's
    closest-quant tiebreak computes 0 bits and cannot prefer anything), then the
    id is only as safe as the repository's file list, which an offline scan cannot
    see. Both are worth telling the operator before a personality bakes one in.
    """
    if ":" not in str(row["id"]):
        return None  # a library-style name, addressed by directory, not by tag
    tag = str(row["id"]).rsplit(":", 1)[-1]
    own = row.get("path") or ""
    picked = loader_pick(siblings or [own], tag)
    if picked and own and picked != own:
        return {"kind": "id-resolves-elsewhere", "tag": tag,
                "would_load": picked.rsplit("/", 1)[-1]}
    if not any(c.isdigit() for c in tag):
        # Nothing here competes for the id right now — the local rule picks this very file. But the
        # tag is not a quantization, so llama.cpp's closest-quant tiebreak cannot *prefer* it, and
        # a sibling that exists only on the hub (invisible to an offline scan) would take the id.
        # Saying "would load <this file>" is the honest version of an empty field.
        here = (picked or own).rsplit("/", 1)[-1] if (picked or own) else ""
        return {"kind": "id-is-not-a-quant", "tag": tag, "would_load": here,
                "picks_this_file": bool(picked) and picked == own}
    return None


def cache_sibling_map(rows: list[dict]) -> dict[str, list[str]]:
    """Repo -> the cache paths we can see for it, for id_round_trip.

    Deliberately local-only. A rescan must not reach the network, so this cannot
    see a sibling that is on the hub but not cached — which is exactly the ukisai
    case. That is why id_round_trip also flags a non-quantization tag on its own:
    the id is unsafe whether or not the competing file is here yet.
    """
    out: dict[str, list[str]] = {}
    for r in rows:
        if r.get("source") != "cache" or not r.get("path"):
            continue
        out.setdefault(str(r["id"]).rsplit(":", 1)[0], []).append(r["path"])
    return out
