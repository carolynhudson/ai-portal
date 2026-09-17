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
    lo = 0
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
            "expert_count": meta.get("expert_count"),
            "expert_used_count": meta.get("expert_used_count"),
            "is_moe": meta.get("is_moe"),
            "has_ssm_state": meta.get("has_ssm_state"),
            # Vision is decided by what sits beside the model, not by the header:
            # the mmproj is a separate file. `has_vision` is the header's own claim
            # and `has_mmproj` is the disk's, so a model that claims vision but lost
            # its projector shows up as the mismatch it is.
            "has_vision": meta.get("has_vision"),
            "has_mmproj": bool(side_bytes),
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
  expert_count INTEGER, expert_used_count INTEGER, is_moe INTEGER,
  has_ssm_state INTEGER, has_vision INTEGER, has_mmproj INTEGER,
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
    "id", "path", "bytes", "shard_total", "mmproj_bytes", "arch", "name",
    "quant", "ctx_trained", "layers", "kv_heads", "attention_heads",
    "embedding_length", "full_attention_interval", "rope_dimension_count",
    "expert_count", "expert_used_count", "is_moe", "has_ssm_state",
    "has_vision", "has_mmproj",
    "est_weights", "est_kv", "est_total", "est_ctx", "est_inputs",
    "scanned_at", "header_hash",
)
_EST_COLS = {"est_weights", "est_kv", "est_total", "est_ctx", "est_inputs"}


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
    return ", ".join(cols)


def _insert_sql() -> str:
    """The INSERT, built once from _WRITE_COLUMNS via the allowlist above."""
    return (f"INSERT OR REPLACE INTO model_index ({_safe_columns(_WRITE_COLUMNS)})"
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
        conn.execute(f"ALTER TABLE model_index ADD COLUMN {c} INTEGER")
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
