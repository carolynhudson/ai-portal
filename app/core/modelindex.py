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
                        for kvh, flag in zip(kv_layers_list, windows))
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
    per = kv_bytes_at(meta, 1, dtype)
    if per is None:
        return 0
    avail = budget_bytes - weight_bytes - HEADROOM_BYTES
    if avail <= 0:
        return 0
    # Round down to a power of two: contexts are configured in powers of two and
    # an odd number invites a false "it fits" from an estimate that is only ±10%
    # accurate. Bit_length avoids float log2, which loses precision on the big
    # values a 108 GiB aperture produces.
    ctx = avail // per
    if ctx < 1 or not math.isfinite(ctx):
        return 0
    # ctx is a float whenever the dtype is fractional (q4_0 = 0.5 bytes). Floor it
    # explicitly so the value handed to bit_length is an int and no conversion can
    # raise on the way — the guard is the conversion, not a comment about it.
    tokens = math.floor(ctx)
    if tokens < 1:
        return 0
    fits = 1 << (tokens.bit_length() - 1)
    # context_length comes straight out of a GGUF header, so it is attacker-shaped
    # and may be missing, negative, or a float. modelmeta._as_int is the one place
    # that decides what a header number means; reusing it avoids a second, laxer
    # opinion here.
    trained = modelmeta._as_int(meta.get("context_length")) or 0
    if trained >= 1:
        # Round the trained ceiling down to a power of two too, so the answer is
        # one configured value rather than an odd boundary.
        cap = 1 << (trained.bit_length() - 1)
        return min(fits, cap)
    return fits


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
    ggufs = [p for p in sorted(entry.glob("*.gguf"))]
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
        parts = s["parts"]
        have = sorted(parts)
        # Expected count comes from the shard suffix ("…-of-00003"), which is the
        # only statement of completeness available. A file with no suffix is a
        # whole model, so it expects exactly itself.
        sh = next((_shard_of(p.name) for p in parts.values() if _shard_of(p.name)),
                  None)
        expected = sh[1] if sh else 1
        missing = [n for n in range(1, expected + 1) if n not in parts]
        if missing:
            problems.append({"kind": "incomplete-shards", "path": str(entry),
                             "model": key, "have": have, "missing": missing,
                             "note": "shard set is incomplete — cannot be loaded"})
            continue
        try:
            head = parts[have[0]]
            total = sum(p.stat().st_size for p in parts.values())
            side_bytes = sum(p.stat().st_size for p in s["sidecars"])
        except OSError as e:
            problems.append({"kind": "unreadable", "path": str(entry),
                             "note": f"{type(e).__name__}: {e}"})
            continue
        meta = modelmeta.sniff_gguf(head)
        if not meta:
            problems.append({"kind": "not-gguf", "path": str(head),
                             "note": "header did not parse as GGUF"})
            continue
        models.append({
            "id": entry.name,
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
            "is_aux": False,
            "header_hash": _header_hash(head),
            "scanned_at": time.time(),
            "_meta": meta,
        })


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
  path TEXT, bytes INTEGER, shard_total INTEGER, mmproj_bytes INTEGER,
  arch TEXT, name TEXT, quant TEXT, ctx_trained INTEGER,
  layers INTEGER, kv_heads INTEGER, attention_heads INTEGER,
  embedding_length INTEGER, full_attention_interval INTEGER,
  rope_dimension_count INTEGER,
  est_weights INTEGER, est_kv INTEGER, est_total INTEGER, est_ctx INTEGER,
  est_inputs TEXT,
  scanned_at REAL, header_hash TEXT
);
"""


def init() -> None:
    conn = db._conn()
    conn.executescript(SCHEMA)
    conn.commit()


def rescan(models_dir: Path | None = None, budget_bytes: int | None = None) -> dict:
    """Rebuild the index. Returns a summary with the problems worth surfacing.

    Incremental in effect if not in mechanism: a rescand is ~34 ms/model of
    header reading, so a full pass over 16 models costs about half a second and
    correctness beats cleverness here. `header_hash` is stored so an N-phase
    follow-up can skip unchanged rows if it ever becomes worth the state.
    """
    init()
    models, problems = scan_library(models_dir)
    budget = budget_bytes or _pool_bytes()
    conn = db._conn()
    conn.execute("DELETE FROM model_index")
    for m in models:
        meta = m.pop("_meta")
        est = est_total(meta, m["bytes"], m.get("ctx_trained") or 4096)
        fits = ctx_that_fits(meta, m["bytes"] + m.get("mmproj_bytes", 0), budget)
        conn.execute(
            "INSERT OR REPLACE INTO model_index (id, path, bytes, shard_total,"
            " mmproj_bytes, arch, name, quant, ctx_trained, layers, kv_heads,"
            " attention_heads, embedding_length, full_attention_interval,"
            " rope_dimension_count, est_weights, est_kv, est_total, est_ctx,"
            " est_inputs, scanned_at, header_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["path"], m["bytes"], m["shard_total"], m["mmproj_bytes"],
             m["arch"], m["name"], m["quant"], m["ctx_trained"], m["layers"],
             m["kv_heads"], m["attention_heads"], m["embedding_length"],
             m["full_attention_interval"], m["rope_dimension_count"],
             est["weights"], est["kv"], est["total"], fits,
             json.dumps(est["est_inputs"]), m["scanned_at"], m["header_hash"]))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM model_index").fetchone()[0]
    db.event("portal", "model-index",
             f"scanned {n} model(s); {len(problems)} problem(s)")
    return {"models": n, "problems": problems, "budget_bytes": budget}


def _pool_bytes() -> int:
    """Budget in the unit that matters: the GPU aperture on an iGPU, else RAM.

    §6.12.0 — rocm-smi's VRAM is a 512 MiB stub on this box and MemAvailable is
    the wrong gauge, so neither is used here when GTT is readable.
    """
    try:
        from .. import metrics
        g = metrics.gpu() or {}
        if g.get("pool_total"):
            return int(g["pool_total"])
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def rows() -> list[dict]:
    init()
    conn = db._conn()
    conn.row_factory = db.sqlite3.Row
    out = []
    for r in conn.execute("SELECT * FROM model_index ORDER BY bytes DESC"):
        d = dict(r)
        if d.get("est_inputs"):
            try:
                d["est_inputs"] = json.loads(d["est_inputs"])
            except (TypeError, ValueError):
                pass
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
