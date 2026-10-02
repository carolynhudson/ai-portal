"""Phase N1: the model index and the memory estimate.

These tests exist because the estimator got three answers wrong while being
written, each plausible and each wrong in a different direction:

* f32 vs f16 KV — a factor of 2 across every number, invisible because the whole
  column moved together (§4.9's table carried this error);
* a per-layer head-count array read as a missing scalar, which silently dropped
  gemma4 and laguna from the estimate entirely;
* sliding-window layers summed over the full context, which priced gemma4's cache
  at 137 GiB for a 16 GB model — and the first fix for it truncated every sliding
  term to zero, the opposite error.

A memory budget that is wrong in the pessimistic direction refuses models that
run fine. Wrong in the optimistic direction loads a box into swap. Both are worse
than "unknown", which is why absent data stays None and no default is invented.

Everything runs against synthetic headers and a temp AI_PORTAL_HOME — never the
real model library, never the network.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

# Throwaway home before the app is imported (same pattern as test_smoke.py).
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-mi-"))
# The two live-provider tests log in with `os.environ["AI_PORTAL_PASSWORD"]`, and this
# module never set one — so they passed only because some earlier module in the same
# process had. Set it here, with setdefault like every other module, so the module is
# self-sufficient when pytest selects it alone.
os.environ.setdefault("AI_PORTAL_PASSWORD", "mi-test-password")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import db, modelindex as mi, modelmeta  # noqa: E402

GiB = 1 << 30


def kv(meta: dict, ctx: int, dtype: str = "f16") -> int:
    """kv_bytes_at with the None case turned into a test failure.

    The estimator returns None for "cannot compute", and every arithmetic
    assertion below wants a number: comparing against None either passes by
    accident or fails with `unsupported operand`, which hides which invariant
    actually broke. Naming the expectation here keeps the assertions readable and
    the failure message about the model, not about Python's operators.
    """
    v = mi.kv_bytes_at(meta, ctx, dtype)
    assert v is not None, f"expected a computable estimate for {meta}"
    return v

# A plain hybrid model: 48 layers, every 4th keeps a KV cache.
HYBRID = {
    "block_count": 48, "attention_count": 24, "attention_kv_count": 2,
    "embedding_length": 2560, "full_attention_interval": 4,
    "context_length": 262144,
}


# ------------------------------------------------------------------ KV math
def test_hybrid_interval_is_applied():
    """full_attention_interval is the difference between a usable model and an
    OOM: at 4, only a quarter of the layers keep a cache."""
    full = dict(HYBRID, full_attention_interval=1)
    assert kv(HYBRID, 1024) * 4 == kv(full, 1024)


def test_kv_dtype_is_an_argument_not_a_constant():
    """§4.9's original table was computed at f32 while llama.cpp runs f16 — a
    silent 2x on every estimate. Naming the dtype makes the assumption visible."""
    f16 = kv(HYBRID, 4096, "f16")
    f32 = kv(HYBRID, 4096, "f32")
    assert f32 == f16 * 2
    assert kv(HYBRID, 4096) == f16, "f16 must stay the default"


def test_kv_is_linear_in_context():
    assert kv(HYBRID, 8192) == 2 * kv(HYBRID, 4096)


def test_missing_field_is_none_not_zero():
    """Zero heads would mean "this model costs nothing for context", which is the
    optimistic error that loads a box into swap."""
    for drop in ("block_count", "attention_kv_count", "embedding_length",
                 "attention_count"):
        m = {k: v for k, v in HYBRID.items() if k != drop}
        assert mi.kv_bytes_at(m, 4096) is None, f"{drop} missing must give None"


def test_rope_dimension_overrides_embedding_division():
    """head_dim comes from rope when the header states it; only fall back to
    embedding / query-heads, which is what llama.cpp does.

    HYBRID gives 2560/24 = 106. A header that says rope=64 must produce the
    smaller cache, not the 106-derived one — otherwise an explicit field is being
    ignored in favour of an inference, which is the wrong precedence.
    """
    base = dict(HYBRID)
    narrow = dict(HYBRID, rope_dimension_count=64)
    wide = dict(HYBRID, rope_dimension_count=256)
    assert kv(narrow, 1024) < kv(base, 1024) < kv(wide, 1024)
    # rope is used directly, so the ratio is exactly head/106, not approximate.
    emb_head = HYBRID["embedding_length"] // HYBRID["attention_count"]
    assert kv(narrow, 1024) * emb_head == kv(base, 1024) * 64


# ---------------------------------------------------- per-layer head arrays
def test_per_layer_kv_array_is_summed():
    """gemma4/laguna spell head counts as one entry per layer. Reading an array as
    a missing scalar dropped these models from the estimate entirely."""
    # 3 layers, no interval, so all three cache. The array says 8+8+4 = 20
    # kv-heads across them; a scalar 20 with the same layer count must price the
    # same. Note the scalar path multiplies block_count x kv_heads, so the
    # equivalent scalar is 20/3 rounded — which is why the honest comparison is
    # against an explicit total, not a per-layer scalar.
    per_layer = {
        "block_count": 3, "attention_count": 4, "embedding_length": 512,
        "context_length": 4096,
        "attention_kv_count_layers": [8, 8, 4],
        "attention_kv_count": None,
    }
    # head_dim = 512/4 = 128; bytes = sum(kvh) * head * 2 (K,V) * 2 (f16) * ctx
    expect = (8 + 8 + 4) * 128 * 2 * 2 * 4096
    assert kv(per_layer, 4096) == expect

    # A uniform array equals the matching scalar (the interval-respecting path).
    uniform = {"block_count": 3, "attention_count": 4, "embedding_length": 512,
               "context_length": 4096,
               "attention_kv_count_layers": [8, 8, 8]}
    as_scalar = {"block_count": 3, "attention_count": 4, "embedding_length": 512,
                 "context_length": 4096, "attention_kv_count": 8}
    assert kv(uniform, 4096) == kv(as_scalar, 4096)


def test_sliding_window_layers_cost_their_window_not_the_context():
    """gemma4 marks 50 of 60 layers sliding with a 1024-token window. Summing
    those over a 131072-token context priced the cache at 137 GiB for a 16 GB
    model — pessimistic enough to refuse a model that runs fine."""
    base = {"block_count": 2, "attention_count": 4, "embedding_length": 512,
            "context_length": 131072, "attention_kv_count_layers": [8, 8]}
    both_full = kv(base, 131072)
    windowed = dict(base, sliding_window=1024,
                    sliding_window_pattern=[True, True])
    w = kv(windowed, 131072)
    assert w == both_full * 1024 // 131072, "both layers capped at the window"
    assert w < both_full / 100

    # Mixed: one windowed layer, one full. The full one dominates.
    mixed = dict(base, sliding_window=1024, sliding_window_pattern=[True, False])
    m = kv(mixed, 131072)
    assert w < m < both_full
    assert m == both_full * (1024 + 131072) // (2 * 131072) * 2 or m > both_full * 0.4


def test_per_layer_array_and_equivalent_scalar_agree():
    """A uniform per-layer array must price the same as the scalar that describes
    it. This is the check that the array path honours full_attention_interval: it
    first summed all 48 entries and overstated the cache by exactly the interval."""
    uniform = dict(HYBRID, attention_kv_count=None,
                   attention_kv_count_layers=[2] * 48)
    assert kv(uniform, 4096) == kv(HYBRID, 4096)


def test_array_path_honours_the_interval_when_array_is_wider():
    """If the array path ignored the interval, 4 entries x 4 heads would price
    like 16 cached layers instead of ceil(4/4)=1."""
    m = {"block_count": 4, "attention_count": 4, "embedding_length": 256,
         "context_length": 1024, "full_attention_interval": 4,
         "attention_kv_count_layers": [2, 2, 2, 2]}
    one_layer_cached = {"block_count": 4, "attention_count": 4,
                        "attention_kv_count": 2, "embedding_length": 256,
                        "context_length": 1024, "full_attention_interval": 4}
    assert kv(m, 1024) == kv(one_layer_cached, 1024)


# ------------------------------------------------------------ fits_ctx rules
def test_ctx_is_capped_at_trained_length():
    """The estimate says the 87 GB model could address >1M tokens. True and
    useless: past its trained context the model is not the model."""
    big_budget = 200 * GiB
    assert mi.ctx_that_fits(HYBRID, 1 * GiB, big_budget) <= HYBRID["context_length"]


def test_ctx_is_a_power_of_two():
    for weight in (0, 10 * GiB, 50 * GiB, 90 * GiB):
        c = mi.ctx_that_fits(HYBRID, weight, 108 * GiB)
        assert c == 0 or c & (c - 1) == 0, f"{c} is not a power of two"


def test_model_that_never_fits_reports_zero():
    """0 is a fact about the box, and it is distinguishable from None (unknown)."""
    assert mi.ctx_that_fits(HYBRID, 200 * GiB, 108 * GiB) == 0
    assert mi.ctx_that_fits({"no_kv_fields": 1}, 100, 108 * GiB) == 0


def test_ctx_shrinks_monotonically_as_weights_grow():
    """Never increases as weights eat the budget, and reaches 0 when weights alone
    exceed it. It legitimately stays at the trained cap while there is headroom —
    HYBRID's cache is tiny, so 100 GiB of weights still leaves room for full
    context. What must not happen is a model with no room reporting a context."""
    budget = 108 * GiB
    seq = [mi.ctx_that_fits(HYBRID, w, budget) for w in
           (1 * GiB, 60 * GiB, 100 * GiB, 107 * GiB, 120 * GiB)]
    assert seq == sorted(seq, reverse=True), seq
    assert seq[0] == HYBRID["context_length"]
    assert seq[-1] == 0


# --------------------------------------------------------------- estimation
def test_estimate_carries_its_inputs():
    """§4.9: a derived number without its inputs cannot be re-derived when the
    formula improves or defended when it is wrong."""
    est = mi.est_total(HYBRID, 10 * GiB, 8192)
    assert set(est["est_inputs"]) >= {"block_count", "attention_kv_count",
                                      "kv_dtype", "ctx", "weight_bytes"}
    assert est["weights"] == 10 * GiB
    assert est["kv"] == kv(HYBRID, 8192)
    assert est["total"] == est["weights"] + est["kv"] + mi.HEADROOM_BYTES


def test_estimate_of_uncomputable_model_has_none_kv_not_zero():
    """A 0 here would sort a model to the front of "what fits" — the optimistic
    error. est_total keeps kv None and total None together."""
    est = mi.est_total({"block_count": 5}, 10 * GiB, 4096)
    assert est["kv"] is None
    assert est["total"] is None


# ----------------------------------------------------------------- scanning
# Type tags as modelmeta's reader numbers them (_SCALAR_FMT keys), which follow
# the legacy GGUF numbering: 4=u32, 5=i32, 7=bool, 8=string, 9=array, 12=f64.
# The modern gguf spec renumbered these (bool 11, u32 12...); using spec numbers
# here makes every value land on the wrong type and read back as 0, which is why
# this table is written out by hand against the reader rather than copied from a
# spec page. If the reader is ever updated to spec numbering, this moves with it.
from gguf_fixture import (GGUF_ARRAY, GGUF_BOOL, GGUF_I32, GGUF_STRING,
                   GGUF_U32, _append_kv_key, _enc_str, _enc_value,
                   _write_fake_gguf, _write_raw_gguf)


def test_scan_collapses_shards_to_one_model_with_total_size(tmp_path):
    """The smoke test once picked shard 1 of a 94 GB model because it was the
    smallest *file*. A shard is a fraction; the index stores the whole."""
    d = tmp_path / "Big-Model"
    d.mkdir()
    one = d / "Big-Model-00001-of-00003.gguf"
    _write_fake_gguf(one, {"block_count": 8, "attention.head_count": 4,
                           "attention.head_count_kv": 2, "embedding_length": 256,
                           "context_length": 4096})
    (d / "Big-Model-00002-of-00003.gguf").write_bytes(b"\0" * 5000)
    (d / "Big-Model-00003-of-00003.gguf").write_bytes(b"\0" * 7000)

    models, problems = mi.scan_library(tmp_path)
    assert problems == [], problems
    assert len(models) == 1
    m = models[0]
    assert m["shard_total"] == 3
    # bytes is the sum of the shards, and path points at part 1 (the header)
    assert m["bytes"] == one.stat().st_size + 5000 + 7000
    assert m["path"].endswith("00001-of-00003.gguf")


def test_incomplete_shard_set_is_a_problem_not_a_model(tmp_path):
    """Silently unloadable is the failure mode: it looks like a model in the
    listing and fails at load time."""
    d = tmp_path / "Broken-Shards"
    d.mkdir()
    _write_fake_gguf(d / "Broken-Shards-00001-of-00003.gguf", {"block_count": 4})
    (d / "Broken-Shards-00002-of-00003.gguf").write_bytes(b"\0" * 100)

    models, problems = mi.scan_library(tmp_path)
    assert models == []
    kinds = [p["kind"] for p in problems]
    assert kinds == ["incomplete-shards"]
    assert problems[0]["missing"] == [3]


def test_dangling_symlink_is_reported_not_raised(tmp_path):
    """Three dangling links exist in the real library and crashed the old
    indexer's os.stat(). A scan of someone else's directory must survive it."""
    (tmp_path / "gone-dir").symlink_to(tmp_path / "does-not-exist")
    d = tmp_path / "Good"
    d.mkdir()
    _write_fake_gguf(d / "Good.gguf", {"block_count": 4, "attention.head_count": 2,
                                       "attention.head_count_kv": 2,
                                       "embedding_length": 128,
                                       "context_length": 1024})
    models, problems = mi.scan_library(tmp_path)
    assert [m["id"] for m in models] == ["Good"]
    assert [p["kind"] for p in problems] == ["dangling-symlink"]


def test_sidecars_are_not_models(tmp_path):
    """mmproj/mtp/draft/imatrix always lose a naive size contest. One shared
    rule with modeladd, and it must not index them as loadable."""
    d = tmp_path / "Vision-Model"
    d.mkdir()
    _write_fake_gguf(d / "Vision-Model.gguf", {"block_count": 4})
    for aux in ("mmproj-vision.gguf", "mtp-draft.gguf", "imatrix-cal.gguf"):
        (d / aux).write_bytes(b"\0" * 9000)
    models, problems = mi.scan_library(tmp_path)
    assert len(models) == 1
    assert models[0]["bytes"] == (d / "Vision-Model.gguf").stat().st_size
    # the sidecar bytes are recorded separately, not added to the model
    assert models[0]["mmproj_bytes"] == 27000


def test_non_gguf_file_is_reported(tmp_path):
    d = tmp_path / "Junk"
    d.mkdir()
    (d / "Junk.gguf").write_text("this is not a gguf file at all")
    models, problems = mi.scan_library(tmp_path)
    assert models == []
    assert problems[0]["kind"] == "not-gguf"


def test_missing_library_dir_is_reported_not_raised(tmp_path):
    models, problems = mi.scan_library(tmp_path / "absent")
    assert models == []
    assert problems[0]["kind"] == "missing-dir"


# --------------------------------------------------------------------- store
def test_rescan_roundtrips_and_is_repeatable(tmp_path, monkeypatch):
    root = tmp_path / "llama"
    (root / "A").mkdir(parents=True)
    _write_fake_gguf(root / "A" / "A.gguf", {"block_count": 8, "attention.head_count": 4,
                                             "attention.head_count_kv": 2,
                                             "embedding_length": 256,
                                             "context_length": 4096})
    db.init()
    r1 = mi.rescan(models_dir=root, budget_bytes=100 * GiB)
    assert r1["models"] == 1
    rows_out = mi.rows()
    assert rows_out[0]["id"] == "A"
    # rows() parses est_inputs back into a dict, so callers never have to know it
    # is stored as TEXT. Asserting the parsed shape is the contract test.
    inputs = rows_out[0]["est_inputs"]
    assert isinstance(inputs, dict), type(inputs)
    assert inputs["ctx"] == 4096
    assert inputs["kv_dtype"] == mi.DEFAULT_KV_DTYPE
    # repeatable: a second scan does not duplicate or drift
    r2 = mi.rescan(models_dir=root, budget_bytes=100 * GiB)
    assert r2["models"] == 1
    assert len(mi.rows()) == 1


def test_fits_query_excludes_unknown_estimates(tmp_path):
    """The whole point of N2: "smallest model that fits in 9 GB" is a query.
    A model with no estimate must never satisfy it."""
    root = tmp_path / "llama"
    (root / "Small").mkdir(parents=True)
    (root / "Mystery").mkdir()
    # estimable and small
    _write_fake_gguf(root / "Small" / "Small.gguf",
                     {"block_count": 8, "attention.head_count": 4,
                      "attention.head_count_kv": 2,
                      "embedding_length": 256,
                      "context_length": 4096})
    # not estimable: no kv fields at all
    _write_fake_gguf(root / "Mystery" / "Mystery.gguf", {"block_count": 8})
    db.init()
    mi.rescan(models_dir=root, budget_bytes=100 * GiB)

    fits = mi.fits_query(20 * GiB)
    ids = [m["id"] for m in fits]
    assert "Small" in ids
    assert "Mystery" not in ids, "an unknown estimate must never be selected"


def test_fits_query_prefers_the_smallest(tmp_path):
    root = tmp_path / "llama"
    a = root / "Tiny"
    b = root / "Large"
    a.mkdir(parents=True)
    b.mkdir()
    _write_fake_gguf(a / "Tiny.gguf", {"block_count": 8, "attention.head_count": 4,
                                       "attention.head_count_kv": 2,
                                       "embedding_length": 256,
                                       "context_length": 4096})
    _write_fake_gguf(b / "Large.gguf", {"block_count": 8, "attention.head_count": 4,
                                        "attention.head_count_kv": 2,
                                        "embedding_length": 256,
                                        "context_length": 4096})
    (b / "Large.gguf").write_bytes((b / "Large.gguf").read_bytes() + b"\0" * 100000)
    db.init()
    mi.rescan(models_dir=root, budget_bytes=100 * GiB)
    fits = mi.fits_query(100 * GiB)
    assert [m["id"] for m in fits][:1] == ["Tiny"]


# --------------------------------------------------- capability facts (N3)
def test_moe_is_a_header_fact_with_its_active_count(tmp_path):
    """`expert_used_count` is what turns MoE from a size fact into a latency fact:
    8 active experts in a 256-expert model is a different machine than 256 dense."""
    d = tmp_path / "llama" / "Moe"
    d.mkdir(parents=True)
    _write_fake_gguf(d / "Moe.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096,
        "expert_count": 256, "expert_used_count": 8})
    m = modelmeta.sniff_gguf(d / "Moe.gguf") or {}
    assert m["expert_count"] == 256
    assert m["expert_used_count"] == 8
    assert m["is_moe"] is True


def test_dense_model_is_not_moe(tmp_path):
    d = tmp_path / "llama" / "Dense"
    d.mkdir(parents=True)
    _write_fake_gguf(d / "Dense.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096})
    m = modelmeta.sniff_gguf(d / "Dense.gguf") or {}
    assert m["expert_count"] is None
    assert not m["is_moe"]


def test_ssm_is_reported_as_state_not_as_a_verdict(tmp_path):
    """Every qwen35 export carries ssm.* keys, including the dense ones, and none
    carries ssm.count. So the field says "this header declares recurrent state" —
    naming it `is_ssm` would invite reading it as "is a Mamba model", which is
    wrong about half the qwen35 family on this box."""
    d = tmp_path / "llama" / "Hybrid"
    d.mkdir(parents=True)
    _write_fake_gguf(d / "Hybrid.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096,
        "ssm.conv_kernel": 4})
    m = modelmeta.sniff_gguf(d / "Hybrid.gguf") or {}
    assert m["has_ssm_state"] is True
    assert "is_ssm" not in m, "the field is a fact, not a classification"


def test_mmproj_and_claimed_vision_are_separate_facts(tmp_path):
    """A model can claim vision and have lost its projector. Collapsing the header's
    claim and the disk's reality into one column makes that invisible, and it is a
    model that will behave differently from what the page says."""
    # scan_library() takes the library root and treats each subdirectory as one
    # model, so the model dir goes directly under tmp_path.
    d = tmp_path / "Blind"
    d.mkdir(parents=True)
    _write_fake_gguf(d / "Blind.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096})
    # `mmproj` is a top-level GGUF key, not an arch-prefixed one. Written through
    # the fixture it would come out as `testarch.mmproj`, which the scanner does not
    # recognise and the file stops being a model at all — so append it unprefixed.
    _append_kv_key(d / "Blind.gguf", "mmproj", True)
    models, problems = mi.scan_library(tmp_path)
    assert problems == [], problems
    assert len(models) == 1
    m = models[0]
    assert m.get("has_vision"), "the header claims it"
    assert not m.get("has_mmproj"), "and the disk disagrees — recorded, not resolved"


def test_a_real_projector_file_sets_has_mmproj(tmp_path):
    """The other half: when the mmproj file is actually present, the disk agrees
    with the header and the bytes are counted as a sidecar rather than as a second
    model."""
    d = tmp_path / "Vision"
    d.mkdir(parents=True)
    _write_fake_gguf(d / "Vision.gguf", {
        "block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096})
    (d / "mmproj-Vision.gguf").write_bytes(b"\0" * 4096)
    models, problems = mi.scan_library(tmp_path)
    assert len(models) == 1
    m = models[0]
    assert m["has_mmproj"]
    assert m["mmproj_bytes"] == 4096
    # and the projector is not added to the model's own weight size
    assert m["bytes"] == (d / "Vision.gguf").stat().st_size


def test_write_columns_and_schema_cannot_drift(tmp_path):
    """The whole reason this table has a derived write list: a column added to the
    row dict and forgotten in the schema stores NULL forever and reads as "this
    model has no value for that". Adding a new fact must fail loudly instead."""
    db.init()
    conn = db._conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    missing = [c for c in mi._WRITE_COLUMNS if c not in cols]
    assert missing == [], f"_WRITE_COLUMNS has no table column for: {missing}"


def test_an_old_table_is_migrated_not_fatal(tmp_path):
    """CREATE TABLE IF NOT EXISTS does not widen a table that is already on disk,
    so every column added since first deploy needs an ALTER. An upgrade must not
    require deleting portal.db."""
    db.init()
    conn = db._conn()
    conn.execute("DROP TABLE model_index")
    conn.execute("""CREATE TABLE model_index (
        id TEXT PRIMARY KEY, path TEXT, bytes INTEGER, shard_total INTEGER,
        mmproj_bytes INTEGER, arch TEXT, name TEXT, quant TEXT,
        ctx_trained INTEGER, layers INTEGER, kv_heads INTEGER,
        attention_heads INTEGER, embedding_length INTEGER,
        full_attention_interval INTEGER, rope_dimension_count INTEGER,
        est_weights INTEGER, est_kv INTEGER, est_total INTEGER, est_ctx INTEGER,
        est_inputs TEXT, scanned_at REAL, header_hash TEXT)""")
    conn.commit()
    before = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    assert "is_moe" not in before
    mi.init()                       # must migrate, not raise
    after = {r[1] for r in db._conn().execute("PRAGMA table_info(model_index)")}
    assert set(mi._WRITE_COLUMNS) <= after, sorted(set(mi._WRITE_COLUMNS) - after)


def test_a_column_name_that_cannot_exist_fails(tmp_path):
    """The migration path adds what it can; what it cannot add is a bug in the
    column list (typo, or absent from SCHEMA too). That must not become a NULL."""
    db.init()
    # ALTER TABLE will happily add a missing-but-legal column (that is the
    # migration path), so the "cannot exist" case has to be a name ALTER rejects.
    monkey_col = mi._WRITE_COLUMNS + ("not a valid column name",)
    original = mi._WRITE_COLUMNS
    try:
        mi._WRITE_COLUMNS = monkey_col
        try:
            mi.init()
        except RuntimeError as e:
            assert "not a valid column name" in str(e)
        else:
            raise AssertionError("a bogus column name must fail the scan")
    finally:
        mi._WRITE_COLUMNS = original


def test_app_startup_populates_the_index(tmp_path, monkeypatch):
    """The regression this test exists for.

    N1 built model_index, N2 built fits_query on top of it, and the smoke test was
    reported as switched over. Nothing created or populated the table, so
    fits_query raised every time, _indexed_candidates caught it, and the code fell
    back to the file walk it was meant to replace. Every test passed because every
    test stubbed the index — a fallback that always fires is not a fallback, it is
    the implementation, and it looks identical from the outside.

    So: start the app the way production does and assert the index is actually
    there afterwards. Not that a call succeeded — that the preferred branch has
    something to serve.
    """
    import time

    import fastapi  # noqa: F401  (imported for its side effects in create_app)

    # A real, small library under the throwaway home.
    root = tmp_path / "llama"
    (root / "Startup").mkdir(parents=True)
    _write_fake_gguf(root / "Startup" / "Startup.gguf",
                     {"block_count": 8, "attention.head_count": 4,
                      "attention.head_count_kv": 2,
                      "embedding_length": 256, "context_length": 4096})
    monkeypatch.setattr(mi.paths, "LLAMA_MODELS", root)
    # The suite turns the startup scan off (tests/conftest.py) because an uncontrolled background scan
    # corrupts other tests. This one is the reason the scan exists, so it turns it back on and waits.
    monkeypatch.setenv("AI_PORTAL_INDEX_STARTUP", "1")

    from app import main as app_main
    conn = db._conn()
    conn.execute("DROP TABLE IF EXISTS model_index")
    conn.commit()

    a = app_main.create_app()
    # Startup scans on a daemon thread; give it a moment rather than sleeping on
    # an assumption about ordering.
    rows = 0
    for _ in range(60):
        try:
            rows = conn.execute("SELECT COUNT(*) FROM model_index").fetchone()[0]
        except Exception:  # noqa: BLE001 — table not created yet
            rows = 0
        if rows:
            break
        time.sleep(0.1)
    assert a is not None
    assert rows == 1, (
        "app startup did not create/populate model_index — readers will fall back "
        "to the directory walk silently, which is the bug this test pins")


def test_index_startup_failure_leaves_readers_working(tmp_path, monkeypatch):
    """A failed scan must degrade, not take the app down — and must say so.

    The counterpart to the test above: the fallback has to keep working when the
    index genuinely cannot be built (library on a dead mount, schema mismatch).
    """
    from app import main as app_main

    def boom(*a, **k):
        raise RuntimeError("no such table: events")

    monkeypatch.setattr(mi, "rescan", boom)
    monkeypatch.setenv("AI_PORTAL_INDEX_STARTUP", "1")   # the degrade path is only real if the scan runs
    a = app_main.create_app()     # must not raise
    assert a is not None
    # and selection still returns something usable via the walk
    root = tmp_path / "llama"
    (root / "Walk").mkdir(parents=True)
    _write_fake_gguf(root / "Walk" / "w.gguf", {"block_count": 8})
    notes: list[str] = []
    app_main.modelindex  # the module the fallback depends on is importable
    assert app_main.modelindex is mi


# ------------------------------------------------------------ verdicts (N3)
def test_verdict_yes_when_it_fits_at_full_context():
    row = {"est_total": 50 * GiB, "est_weights": 49 * GiB,
           "est_ctx": 262144, "ctx_trained": 262144}
    assert mi.verdict(row, 108 * GiB) == "yes"


def test_verdict_never_when_the_weights_alone_do_not_fit():
    row = {"est_total": 200 * GiB, "est_weights": 200 * GiB,
           "est_ctx": 0, "ctx_trained": 4096}
    assert mi.verdict(row, 108 * GiB) == "never"


def test_verdict_reduced_when_only_a_shorter_context_fits():
    row = {"est_total": 60 * GiB, "est_weights": 49 * GiB,
           "est_ctx": 8192, "ctx_trained": 262144}
    assert mi.verdict(row, 55 * GiB) == "reduced"


def test_unknown_is_not_never():
    """"I could not price it" and "this cannot run" produce the same blank cell and
    opposite actions. Merging them teaches a reader to distrust the column, and an
    unknown model must never be auto-selected."""
    assert mi.verdict({"est_total": None, "est_weights": None}, 108 * GiB) == "unknown"
    assert mi.verdict({"est_total": 200 * GiB, "est_weights": 200 * GiB},
                      108 * GiB) == "never"


def test_verdict_is_unknown_when_the_pool_cannot_be_read():
    """No readable aperture means no claim, not "it fits" and not "it does not"."""
    row = {"est_total": 10 * GiB, "est_weights": 9 * GiB,
           "est_ctx": 32768, "ctx_trained": 32768}
    assert mi.verdict(row, 0) == "unknown"


def test_reduced_is_not_a_power_of_two_rounding_artifact():
    """The bug this pins. ctx_that_fits() rounds DOWN to a power of two, so a model
    trained at 40960 tokens can never report est_ctx >= 40960 — comparing against
    the raw trained length called it "reduced" on this box's own 8B embedding model
    and told the user to run at 32K when the hardware fits it at 40K. The page
    recommending a smaller context than the box supports is worse than no page."""
    non_pow2 = {"est_total": 10 * GiB, "est_weights": 9 * GiB,
                "est_ctx": 32768,          # 40960 rounded down: this is the ceiling
                "ctx_trained": 40960}
    assert mi.verdict(non_pow2, 108 * GiB) == "yes"
    # and a genuinely shorter cap on the same model is still reported
    genuinely_short = dict(non_pow2, est_ctx=8192)
    assert mi.verdict(genuinely_short, 108 * GiB) == "reduced"


def test_ctxlen_filter_shape():
    """131072 must read as a context length, not a file size: _human() would print
    "128K" and a reader would take it for bytes."""
    from app.main import _ctxlen
    assert _ctxlen(4096) == "4K"
    assert _ctxlen(131072) == "128K"
    assert _ctxlen(1048576) == "1.0M"
    assert _ctxlen(512) == "512"
    assert _ctxlen(None) == "—"
    # 0 is a real value here ("never fits") and must not vanish into the em dash
    assert _ctxlen(0) == "0"
    # and never raise, whatever a GGUF header claimed
    for junk in ("nonsense", float("inf"), float("nan"), object(), True):
        assert isinstance(_ctxlen(junk), str)


# ------------------------------------------- ctx_that_fits on a saturating curve
def test_ctx_that_fits_uses_the_real_curve_not_a_one_token_slope():
    """The bug this pins. `avail // kv_bytes_at(meta, 1)` treated KV as linear in
    ctx. It is not: on a sliding-window model the layers under the window stop
    growing once ctx passes it, so gemma-4 charges all 60 layers at ctx=1 and only
    10 of them at ctx=262144. The one-token slope was therefore ~2x the true cost at
    scale and capped this 31B model at 131072 while 84 GiB sat free and its entire
    262144 cache costs 7 GiB. The error pointed the safe way, which is exactly why
    it survived: an estimate that under-promises looks like caution, not a bug."""
    gemma = {
        "context_length": 262144, "block_count": 60, "embedding_length": 5376,
        "attention_count_layers": [64] * 60,
        "attention_kv_count_layers": [16] * 60,
        "sliding_window": 1024,
        "sliding_window_pattern": [True] * 5 + [False] + [True] * 4 + [False] + [True] * 49,
    }
    weights = 16 * GiB
    # per-token cost must fall as ctx grows, or this test's premise is wrong.
    # kv_bytes_at returns None when it cannot price a model, and a fixture that
    # cannot be priced would make every assertion below pass vacuously on 0 — so
    # the None case is a failure of the fixture, stated as one.
    at_1 = mi.kv_bytes_at(gemma, 1)
    at_262k = mi.kv_bytes_at(gemma, 262144)
    assert at_1 and at_262k, "fixture is unpriceable; the estimator cannot see it"
    assert at_262k / 262144 < at_1 / 1.5, \
        "KV is not saturating; the fixture is not a window model"

    got = mi.ctx_that_fits(gemma, weights, 108 * GiB)
    assert got == 262144, f"expected the trained 262144, got {got}"


def test_ctx_that_fits_still_respects_memory_on_a_dense_model():
    """The fix must not become an excuse to be optimistic. A dense model's KV grows
    linearly, so a small budget still has to produce a small context — and no
    budget at all may exceed what it was trained for."""
    dense = {"context_length": 262144, "block_count": 32, "embedding_length": 4096,
             "attention_count": 32, "attention_kv_count": 8}
    assert mi.ctx_that_fits(dense, 1 * GiB, 16 * GiB) == 65536
    assert mi.ctx_that_fits(dense, 1 * GiB, 64 * GiB) == 262144
    # infinite RAM does not buy context past training
    assert mi.ctx_that_fits(dense, 1 * GiB, 1 << 50) == 262144
    assert mi.ctx_that_fits(dense, 500 * GiB, 108 * GiB) == 0


def test_ctx_that_fits_priced_at_the_trained_cap_when_it_sits_between_rungs():
    """A model trained at 1000 tokens is not unloadable just because 1000 is not a
    power of two. The ladder floors to 512; the direct price at the cap catches the
    case the ladder would round away."""
    dense = {"context_length": 1000, "block_count": 4, "embedding_length": 1024,
             "attention_count": 8, "attention_kv_count": 1}
    assert mi.ctx_that_fits(dense, 1 * GiB, 108 * GiB) == 512


def test_ctx_that_fits_is_monotonic_in_budget():
    """A budget increase that lowers the answer means two code paths disagree about
    what fits, and the page would show one model getting worse as the box idled."""
    dense = {"context_length": 262144, "block_count": 32, "embedding_length": 4096,
             "attention_count": 32, "attention_kv_count": 8}
    prev = 0
    for e in range(30, 44):        # 1 GiB .. 8 TiB
        got = mi.ctx_that_fits(dense, 1 * GiB, 1 << e)
        assert got >= prev, f"budget 2^{e} gave {got} < previous {prev}"
        prev = got


# ------------------------------------------------ peak estimate + live identity
def test_peak_bytes_is_the_number_a_loader_needs():
    row = {"est_weights": 16 * GiB, "est_kv": 7 * GiB, "est_ctx": 262144}
    peak = mi.peak_bytes(row)
    assert peak == 16 * GiB + 7 * GiB + mi.HEADROOM_BYTES


def test_peak_is_stricter_than_the_displayed_verdict_by_exactly_headroom():
    """The two numbers measure different questions, and the difference is load-bearing.

    `verdict()` answers "is weights+cache within the aperture" — a display fact.
    `peak_bytes()` answers "what is resident if this loads", and adds HEADROOM_BYTES
    because the aperture is not the only consumer of RAM. So a model can be labelled
    "fits" at a budget that peak_bytes says is not enough to actually load it.

    That gap is the point, and it must be exactly HEADROOM_BYTES: if the two ever
    drift further apart, either the page is advertising models the loader refuses, or
    the loader refuses models that genuinely fit. Pinning the difference is what makes
    a future change to either one an explicit decision instead of a silent one.
    """
    weights, kv = 16 * GiB, 7 * GiB
    row = {"est_weights": weights, "est_kv": kv, "est_total": weights + kv,
           "est_ctx": 262144, "ctx_trained": 262144}
    peak = mi.peak_bytes(row)
    # State the None case as a failure of the fixture, then arithmetic on `peak` is
    # both safe at runtime and provably safe to a reader of this file.
    assert peak is not None, "peak_bytes refused to price a fully-specified row"
    assert peak - (weights + kv) == mi.HEADROOM_BYTES

    # verdict's own boundary is est_total, one headroom below the loader's boundary.
    assert mi.verdict(row, weights + kv) == "yes"
    assert mi.verdict(row, weights + kv - 1) == "reduced"
    # and the loader's boundary is affordable by the display rule too, which is the
    # only direction that is safe: peak being affordable implies est_total is.
    assert mi.verdict(row, peak) == "yes"


def test_peak_bytes_refuses_to_guess():
    """A scheduler given 0 will load; given None it must refuse. Returning a number
    for a model we cannot price is the difference between a refusal and a swap."""
    assert mi.peak_bytes({"est_weights": 0}) is None
    assert mi.peak_bytes({"est_weights": 8 * GiB, "est_kv": None, "est_ctx": 4096}) is None
    assert mi.peak_bytes({"est_weights": 8 * GiB, "est_kv": 10**9, "est_ctx": 0}) is None


def test_peak_bytes_survives_hostile_numbers():
    """Header-derived values arrive unfiltered. inf/NaN must become None, not an
    OverflowError that escapes into a page render or a scheduler's arithmetic."""
    for junk in (float("inf"), float("nan")):
        assert mi.peak_bytes({"est_weights": junk, "est_kv": 1, "est_ctx": 1024}) is None
    # a ctx that is not a number at all must not raise on comparison either
    for junk in ("1024", object(), None):
        assert mi.peak_bytes({"est_weights": 10**10, "est_kv": 10**9, "est_ctx": junk}) is None


# -------------------------------------- re-pricing a stored row at another context
def _scanned_row(tmp_path, kv: dict, model: str = "A") -> dict:
    """One synthetic model, scanned and stored, returned as the row `rows()` hands out."""
    root = tmp_path / "llama"
    (root / model).mkdir(parents=True)
    _write_fake_gguf(root / model / f"{model}.gguf", kv)
    db.init()
    mi.rescan(models_dir=root, budget_bytes=100 * GiB)
    return mi.rows()[0]


_DENSE_KV = {"block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
             "embedding_length": 256, "context_length": 4096}

_WINDOW_KV = dict(_DENSE_KV, **{
    "attention.head_count_kv": [16, 16, 4, 4],
    "attention.sliding_window": 1024,
    "attention.sliding_window_pattern": [True, True, False, False],
})


def test_a_stored_row_can_be_repriced_at_a_smaller_context(tmp_path):
    """peak_bytes()'s re-pricing branch used to hand the scan row straight to kv_bytes_at(), which
    reads `block_count` — a fact the table stores as `layers`. It found nothing and returned None
    for all 16 models on this box, so "what would this cost at a smaller context" has never been a
    question this index could answer.

    Arithmetic, not a range: 8 layers x 2 kv heads x 64 (256/4) x 2 (K,V) x 2 bytes x 1024 tokens.
    """
    row = _scanned_row(tmp_path, _DENSE_KV)
    small = mi.peak_bytes(row, 1024)
    assert small is not None, "re-pricing returned None, as it did for every real model here"
    assert small == row["est_weights"] + 8 * 2 * 64 * 2 * 2 * 1024 + mi.HEADROOM_BYTES


def test_est_inputs_records_the_per_layer_shape_it_priced_with(tmp_path):
    """`est_inputs` claims to hold the inputs the estimate used. For a model whose head counts are
    per-layer arrays it did not: gemma-4 and Laguna were priced by geometry that was never stored
    anywhere, so their own estimates could not be reproduced or checked by anything.
    """
    row = _scanned_row(tmp_path, _WINDOW_KV)
    inputs = row["est_inputs"]
    assert inputs["attention_kv_count_layers"] == [16, 16, 4, 4]
    assert inputs["sliding_window_pattern"] == [True, True, False, False]
    # Per layer, sliding layers capped at their own window: (16*1024 + 16*1024 + 4*4096 + 4*4096)
    # x 64 (head width, embedding 256 / 4 query heads) x 2 (K,V) x 2 bytes = 16 MiB at 4096.
    assert row["est_kv"] == (16 * 1024 + 16 * 1024 + 4 * 4096 + 4 * 4096) * 64 * 2 * 2


def test_repricing_a_sliding_window_row_repeats_the_window_caps(tmp_path):
    """Re-pricing must re-run the windowed sum at the new context, not scale the answer.

    At 512 the truth is (16*512 + 16*512 + 4*512 + 4*512) x 64 x 2 x 2 = 5 MiB, while scaling the
    stored 16 MiB by 512/4096 says 2 MiB — 2.5x too small. This box shows the same effect at 3.2x
    on gemma-4 (§6.12.5), which is why the fallback in personality.py refuses to price a smaller
    context below the estimate made at a larger one.
    """
    row = _scanned_row(tmp_path, _WINDOW_KV)
    exact = mi.repriced_kv(row, 512)
    assert exact == (16 * 512 + 16 * 512 + 4 * 512 + 4 * 512) * 64 * 2 * 2
    assert exact > row["est_kv"] * 512 // row["est_inputs"]["ctx"], \
        "linear scaling understated a sliding-window model — the direction that ships an OOM"


# --------------------------------- host RAM: prompt cache and context checkpoints
def test_host_cache_is_priced_from_measured_defaults(tmp_path):
    """llama.cpp runs both of these by default and the aperture estimate contains neither: an 8192
    MiB prompt cache and up to 32 checkpoint copies per slot.

    One checkpoint is priced as the model's KV for `CHECKPOINT_TOKEN_BOUND` tokens: for this fixture
    that is 16 MiB, its whole cache at 4096 tokens with the window caps applied, so the bound is
    exact-by-construction against this row rather than against some other model's measurement.

    On the real models the bound was measured against (§6.12.6) it sits above what the server
    reported -- 28.0 MiB priced against 19.266 MiB for Qwen3.5-0.8B (1.45x) and 128.0 MiB against
    50.251 MiB for Qwythos-9B (2.55x). Above is the side worth being on for a capacity check, but
    the two ratios are not one ratio, and the bound is not derived from the mechanism -- it is a
    token-count guess that happens to be conservative on both models measured so far.
    """
    row = _scanned_row(tmp_path, _WINDOW_KV)
    h = mi.host_cache_bytes(row)
    assert h["prompt_cache_bytes"] == 8192 * 1024 * 1024
    assert h["checkpoint_eligible"] is True
    assert h["checkpoint_bytes"] == 32 * 16 * 2 ** 20
    assert h["total_bytes"] == (8192 + 32 * 16) * 2 ** 20


def test_checkpoint_cost_is_per_slot_not_per_server(tmp_path):
    """`n_ctx_checkpoints` is documented as per slot, so four slots cost four times the copies."""
    row = _scanned_row(tmp_path, _WINDOW_KV)
    one = mi.host_cache_bytes(row, parallel=1)["checkpoint_bytes"]
    four = mi.host_cache_bytes(row, parallel=4)["checkpoint_bytes"]
    assert four == 4 * one
    assert mi.host_cache_bytes(row, parallel=-1)["checkpoint_bytes"] == one, \
        "parallel = -1 is llama.cpp's auto, not a negative slot count"


def test_a_dense_model_is_not_charged_for_checkpoints_it_would_not_create(tmp_path):
    """llama.cpp declines checkpoints per model, not per setting (server-context.cpp: needs SWA, or
    sequence removal that is full/bounded). Measured here: `--ctx-checkpoints 32` on a dense Mistral
    logged `main/do_checkpoint = no` and produced none, while two interval-4 archs checkpointed at
    every boundary.

    The decline depends on the KV cache's runtime capability, which a GGUF header does not state, so
    the zero is recorded as an unpriced assumption in `notes` rather than presented as a measurement.
    """
    row = _scanned_row(tmp_path, _DENSE_KV)
    h = mi.host_cache_bytes(row)
    assert h["checkpoint_eligible"] is False
    assert h["checkpoint_bytes"] == 0
    assert h["total_bytes"] == 8192 * 1024 * 1024
    assert any("declines checkpoints" in n for n in h["notes"])


def test_a_hybrid_layout_makes_a_model_checkpoint_eligible(tmp_path):
    """`full_attention_interval > 1` is the layout llama.cpp checkpoints for, and it is the fact the
    index already keeps, so eligibility is read from the index rather than guessed from the arch name.
    """
    row = _scanned_row(tmp_path, dict(_DENSE_KV, **{"full_attention_interval": 4}))
    assert mi.checkpoint_eligible(row) is True


def test_cache_ram_limits_are_priced_and_an_unbounded_one_is_not(tmp_path):
    """0 disables the cache and is priced at zero; -1 removes the limit and cannot be priced at all.

    The `-1` case returns None rather than a big number because the point of the price is to be
    compared against installed RAM, and "as much as it likes" has no comparison.
    """
    row = _scanned_row(tmp_path, _DENSE_KV)
    off = mi.host_cache_bytes(row, cache_ram_mib=0)
    assert off["prompt_cache_bytes"] == 0
    assert off["total_bytes"] == 0, "cache off and no checkpoints: nothing to hold"

    unbounded = mi.host_cache_bytes(row, cache_ram_mib=-1)
    assert unbounded["prompt_cache_bytes"] is None
    assert unbounded["total_bytes"] is None
    assert any("no limit" in n for n in unbounded["notes"])

    halved = mi.host_cache_bytes(row, cache_ram_mib=2048)
    assert halved["total_bytes"] == 2048 * 1024 * 1024


def test_repricing_refuses_when_the_stored_estimate_is_not_reproducible(tmp_path):
    """A row whose own inputs cannot explain its stored number is a row whose geometry was never
    recorded. Handing back the scalar approximation instead is exactly the failure this index exists
    to prevent: a plausible number that is not the right one.
    """
    row = _scanned_row(tmp_path, _DENSE_KV)
    row["est_kv"] += 1
    assert mi.repriced_kv(row, 1024) is None
    assert mi.peak_bytes(row, 1024) is None, "an unreproducible estimate must not be priced"


def test_headroom_after_is_what_a_load_leaves_behind():
    """Router planning is a sequence. `--models-max 1` today; a policy that keeps two
    warm needs to know what a load costs the next one."""
    row = {"est_weights": 10 * GiB, "est_kv": 5 * GiB, "est_ctx": 8192}
    budget = 100 * GiB
    peak = mi.peak_bytes(row)
    assert peak is not None
    assert mi.headroom_after(row, budget) == budget - peak
    # unknown consequences, not zero consequences
    assert mi.headroom_after({"est_weights": 10 * GiB}, budget) is None


def test_router_key_matches_a_library_dir_to_a_router_alias():
    """The router registers some models under a HuggingFace-style alias. Matching on
    raw strings left a live model looking not-running, which is the one thing a "live"
    indicator must never do: a wrong "not running" invites loading a second copy of a
    model that is already serving traffic."""
    assert mi.router_key("gemma-4-31B-it-QAT-Q4_0") == mi.router_key("gemma-4-31B-it-QAT-Q4_0")
    assert mi.router_key("unsloth/Qwen3.8-27B-GGUF:Q8_K_XL") == mi.router_key("Qwen3.8-27B-GGUF")
    assert mi.router_key("Qwen3.8-27B-GGUF:Q8_K_XL") == mi.router_key("qwen3.8.27b.gguf")


def test_router_key_keeps_different_quants_apart():
    """One file under two spellings collapses; two different files must not. Q4 and Q8
    of one architecture are different weights with different costs."""
    assert mi.router_key("Model-Q4_K_M") != mi.router_key("Model-Q8_0")


def test_live_keys_ignores_the_unreachable_router_placeholder():
    """`models_live()` returns a synthetic row carrying an error message when the
    router is down. Treating that as a model id would show an error string as live."""
    keys = mi.live_keys([{"id": "(router unreachable: boom)"}, {"id": "Real-Model"}])
    assert list(keys) == [mi.router_key("Real-Model")]


def test_declared_type_agrees_with_schema():
    """The migration used to ALTER every missing column in as INTEGER. template_markers
    is TEXT in SCHEMA, so a fresh install and an upgraded one would have carried the
    same column with different declared types — and SQLite stores a string in an
    INTEGER column without complaining, so nothing would ever report it."""
    for col, want in (("template_markers", "TEXT"), ("has_chat_template", "INTEGER"),
                      ("bytes", "INTEGER"), ("scanned_at", "REAL"),
                      ("est_inputs", "TEXT")):
        assert mi._declared_type(col) == want, col
    # a column SCHEMA does not mention falls back to the type that can hold anything
    assert mi._declared_type("invented_column") == "TEXT"


def test_init_migrates_a_table_that_predates_the_new_columns():
    """CREATE TABLE IF NOT EXISTS does not widen a table already on disk. A deployed
    database from before these columns existed has to gain them, with the type SCHEMA
    declares, or the capability facts read as NULL forever."""
    import sqlite3
    db.init()
    old_shape = """CREATE TABLE model_index (
      id TEXT PRIMARY KEY, path TEXT, bytes INTEGER, shard_total INTEGER,
      mmproj_bytes INTEGER, arch TEXT, name TEXT, quant TEXT, ctx_trained INTEGER,
      layers INTEGER, kv_heads INTEGER, attention_heads INTEGER,
      embedding_length INTEGER, full_attention_interval INTEGER,
      rope_dimension_count INTEGER, expert_count INTEGER, expert_used_count INTEGER,
      is_moe INTEGER, has_ssm_state INTEGER, has_vision INTEGER, has_mmproj INTEGER,
      est_weights INTEGER, est_kv INTEGER, est_total INTEGER, est_ctx INTEGER,
      est_inputs TEXT, scanned_at REAL, header_hash TEXT);"""
    conn = sqlite3.connect(str(db.DB))
    conn.execute("DROP TABLE IF EXISTS model_index")
    conn.execute(old_shape)
    conn.commit()
    before = {r[1] for r in conn.execute("PRAGMA table_info(model_index)")}
    conn.close()
    assert "template_markers" not in before

    mi.init()

    conn = sqlite3.connect(str(db.DB))
    info = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(model_index)")}
    conn.close()
    assert info.get("template_markers") == "TEXT"
    assert info.get("has_chat_template") == "INTEGER"


# ------------------------------------------------------- the live light (N3 merge)
def _fake_provider(monkeypatch, live):
    """Make models_live() return `live`, on the real provider object.

    Patched on the class, not by substituting a stub: the /models page and the page it
    redirects through reach for behind(), provenance(), jobs() and status(). A stub
    with only the two methods wanted fails inside an unrelated panel and the failure
    looks like a bug in the route under test.
    """
    from app import providers
    from app.providers.llamacpp import LlamaCpp

    monkeypatch.setattr(LlamaCpp, "models_live", lambda self: list(live))
    monkeypatch.setattr(LlamaCpp, "models_scan", lambda self: [])
    providers.get_llamacpp()   # ensure the accessor is warm and the same object


def _seed_library(monkeypatch, names):
    """Put real GGUFs where the portal already looks, then rescan.

    Two reasons this seeds the *existing* library path rather than a fresh tmp_path:

    * create_app() starts a daemon thread that calls rescan() with no argument, which
      scans paths.LLAMA_MODELS and DELETEs every row first. A test that seeds a
      different directory, or inserts rows by hand, has its fixture erased by the very
      startup path it is testing — and races it, which is how these tests first failed
      with "database is locked".
    * Seeding the path the app will scan means the test passes whether the rows arrive
      from the test's rescan or the startup thread's, which is the honest ordering.
    """
    root = mi.paths.LLAMA_MODELS
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)
        _write_fake_gguf(root / name / f"{name}.gguf",
                         {"block_count": 8, "attention.head_count": 4,
                          "attention.head_count_kv": 2,
                          "embedding_length": 256, "context_length": 4096})
    # Rescan here rather than relying on the app's warm-up thread to do it first. The docstring above
    # spells out why racing that thread is not fixable from the test's side; not depending on it is.
    mi.rescan()
    return root


def test_live_light_matches_a_library_row_through_the_alias_rule(monkeypatch):
    """The merged table's one risky column.

    A model the router registers under a HuggingFace-style alias must still light up
    its library row. A light that says "not running" about a model that is serving
    traffic invites loading a second copy of it, which is precisely how you double the
    memory cost of a model that is already up.

    Goes through the real route with a real rescanned index, not a helper: the claim
    under test is that the page and the router agree, and a unit test of router_key()
    alone cannot show the page used it.
    """
    from fastapi.testclient import TestClient

    from app import main as app_main

    _seed_library(monkeypatch, ["Gemma-4-31B"])
    _fake_provider(monkeypatch, [{"id": "org/Gemma-4-31B:Q4_0", "object": "model"}])

    c = TestClient(app_main.create_app())
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]})
    body = c.get("/models").text
    assert "registered with the router" in body, "no live light rendered at all"
    assert "as org/Gemma-4-31B:Q4_0" in body, "the alias was hidden instead of shown"
    assert "Live but not in the library" not in body, \
        "a model that matches a library row was filed as unaccounted"


def test_live_models_outside_the_library_are_listed_not_dropped(monkeypatch):
    """The merge must not quietly become a filter.

    A model the router serves with no matching directory is a fact an operator needs.
    Dropping it makes the merged table look complete when it is not, and the omission
    is invisible precisely because everything else on the page is correct.
    """
    from fastapi.testclient import TestClient

    from app import main as app_main

    _seed_library(monkeypatch, ["Known-Model"])
    _fake_provider(monkeypatch, [{"id": "Known-Model", "object": "model"},
                                 {"id": "stranger/outside-weights:Q8", "object": "model"}])

    c = TestClient(app_main.create_app())
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]})
    body = c.get("/models").text
    assert "stranger/outside-weights:Q8" in body, "an unaccounted live model vanished"
    assert "Live but not in the library (1)" in body



def test_the_models_page_says_what_a_model_can_be_used_for(monkeypatch):
    """The capability badges have to reach the page, not just the table.

    The stored columns are the file's own facts; the badges are the portal's
    reading of them, and the page is where an operator picks which model goes in
    an autocomplete slot. This is pinned because the break is silent: the template
    iterates `m.caps or []`, so a route that stops computing caps renders a row
    with a blank cell and every other cell still correct.
    """
    from fastapi.testclient import TestClient

    from app import main as app_main

    root = mi.paths.LLAMA_MODELS
    root.mkdir(parents=True, exist_ok=True)
    head = {"general.architecture": "testarch"}
    body_kv = [("testarch.block_count", 8), ("testarch.embedding_length", 256),
               ("testarch.context_length", 4096),
               ("testarch.attention.head_count", 4),
               ("testarch.attention.head_count_kv", 2)]
    for name, pairs in (("Autocomplete-Model",
                         list(head.items()) + body_kv
                         + [("testarch.pooling_type", 1),
                            # Built in pieces: the literal `<|name|>` form has a
                            # way of arriving here as an empty string (see
                            # test_model_signals._special), and a fixture token
                            # that silently vanishes tests nothing.
                            ("tokenizer.ggml.tokens",
                             ["a", "b", "<" + "|" + "fim_middle" + "|" + ">"])]),
                        ("Promises-A-Head",
                         list(head.items()) + body_kv
                         + [("testarch.nextn_predict_layers", 1),
                            ("tokenizer.ggml.tokens", ["a", "b", "c"])])):
        (root / name).mkdir(parents=True, exist_ok=True)
        _write_raw_gguf(root / name / f"{name}.gguf", pairs)
    mi.rescan()

    c = TestClient(app_main.create_app())
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]})
    page = c.get("/models").text
    assert 'title="tokenizer declares fill-in-the-middle specials' in page, \
        "the autocomplete badge did not render"
    assert ">autocomplete<" in page
    assert ">embedder<" in page, "pooling_type 1 did not reach the page"
    assert ">no FIM<" in page, "a model without FIM specials rendered as if silent"
    assert ">head missing<" in page, \
        "a header promising a draft head the weights lack went unmentioned"


# ------------------------------------------------------ the Hugging Face cache
#
# The runtime discovers these on its own: measured on the deployed build, a router
# started with --models-dir pointed elsewhere still listed every cache repo as
# `[cache] org/repo:QUANT`. So the naming rules in scan_cache() are not the
# portal's invention — they are transcribed from llama.cpp, and a drift between
# them means the portal writes preset sections the router will never read.

_REV_A = "a" * 40
_REV_B = "b" * 40
_KV = {"block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
       "embedding_length": 256, "context_length": 4096}


def _hub_repo(hub: Path, repo: str, revision: str, files: dict[str, dict],
              *, refs: str | None = "main") -> Path:
    """One repo in a fake hub cache, laid out the way `hf download` lays it out.

    `refs=None` builds the state a half-finished download leaves behind: files on
    disk and no pointer to which revision is current.
    """
    folder = hub / f"models--{repo.replace('/', '--')}"
    snap = folder / "snapshots" / revision
    for name, kv in files.items():
        p = snap / name
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_fake_gguf(p, kv or _KV, pad=len(name))
    if refs is not None:
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        (folder / "refs" / refs).write_text(revision + "\n")
    return snap


def _fake_mmproj(path: Path, *, vision: bool = True) -> None:
    """A projector fixture that declares what a projector declares.

    `general.type` is a top-level string key and `clip.has_vision_encoder` is a
    top-level bool; written through _write_fake_gguf they would come out arch-
    prefixed (`testarch.general.type`), the reader would see a projector that
    declares nothing, and sidecar_type would be None for a reason that has
    nothing to do with the code under test.
    """
    _write_fake_gguf(path, {"block_count": 1}, pad=0)
    _append_kv_string(path, "general.type", "mmproj")
    if vision:
        _append_kv_key(path, "clip.has_vision_encoder", True)


def _append_kv_string(path: Path, key: str, value: str) -> None:
    """_append_kv_key, for a string value instead of a boolean."""
    raw = bytearray(path.read_bytes())
    n = struct.unpack_from("<Q", raw, 16)[0]
    struct.pack_into("<Q", raw, 16, n + 1)
    path.write_bytes(bytes(raw) + _enc_str(key) + _enc_value(value))


def test_a_cache_repo_is_named_exactly_as_the_router_names_it(tmp_path):
    """The id is the whole point.

    A preset section configures the model whose name it matches, so an id the
    router does not use is a settings block that silently does nothing — and a
    budget computed from it prices a model nobody can load.
    """
    hub = tmp_path / "hub"
    snap = _hub_repo(hub, "org/Muse-GGUF", _REV_A, {
        "IQ3_S/muse-IQ3_S-00001-of-00002.gguf": _KV,
        "IQ3_S/muse-IQ3_S-00002-of-00002.gguf": _KV,
        "mmproj-BF16.gguf": _KV,
    })
    _fake_mmproj(snap / "mmproj-BF16.gguf")

    models, problems = mi.scan_cache(hub)
    assert problems == [], problems
    assert [m["id"] for m in models] == ["org/Muse-GGUF:IQ3_S"]
    m = models[0]
    assert m["source"] == "cache"
    assert m["shard_total"] == 2, "two shards, one model — the part set sums to one cost"
    assert m["mmproj_bytes"] > 0, "the projector is real memory and has to be priced"
    assert m["has_mmproj"] and m["has_vision"]


def test_the_quant_tag_is_read_the_way_llama_cpp_reads_it(tmp_path):
    """Upper-casing, dot separators, and the one case that is not a model at all.

    get_gguf_split_info() takes the trailing `[-.]TOKEN` and upper-cases it, and
    drops a file with no such token. A scanner that guessed "the directory name is
    the quant" would name these wrongly, and the wrongness shows up as a preset
    section that matches nothing.
    """
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Odd-GGUF", _REV_A, {
        "plain-q4_k_m.gguf": _KV,      # lower case in the filename
        "other.F16.gguf": _KV,         # dot before the tag, not dash
        "no_quant_here.gguf": _KV,     # no tag: llama.cpp does not offer it
    })

    models, problems = mi.scan_cache(hub)
    assert problems == [], problems
    assert sorted(m["id"] for m in models) == ["org/Odd-GGUF:F16", "org/Odd-GGUF:Q4_K_M"]


def test_only_the_revision_refs_names_is_scanned(tmp_path):
    """A stale snapshot directory is not a second model.

    hf-cache.cpp resolves refs/main and walks only that revision. Listing the
    leftovers of an older one would put a model on the page that no load can
    reach — and if it shares a quant tag, it would look like a duplicate id.
    """
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Muse-GGUF", _REV_A, {"muse-IQ3_S.gguf": _KV},
              refs=None)
    _hub_repo(hub, "org/Muse-GGUF", _REV_B, {"muse-IQ3_S.gguf": _KV}, refs=None)
    (hub / "models--org--Muse-GGUF" / "refs" / "main").parent.mkdir(parents=True,
                                                                     exist_ok=True)
    (hub / "models--org--Muse-GGUF" / "refs" / "main").write_text(_REV_B + "\n")

    models, problems = mi.scan_cache(hub)
    assert problems == [], problems
    assert len(models) == 1
    assert Path(models[0]["path"]).parent.name == _REV_B


def test_a_repo_with_no_readable_ref_is_reported_not_guessed(tmp_path):
    """Files on disk, no pointer to which revision is current.

    Neither the runtime nor the portal can know which snapshot is live, so the
    model is not offered — and the reason is said out loud rather than the cache
    simply looking empty."""
    hub = tmp_path / "hub"
    _hub_repo(hub, "someone/NoRef-GGUF", _REV_A, {"thing-Q4_K_M.gguf": _KV},
              refs=None)

    models, problems = mi.scan_cache(hub)
    assert models == []
    assert [p["kind"] for p in problems] == ["no-ref"]


def test_a_projector_or_draft_file_is_never_a_model_of_its_own(tmp_path):
    """mmproj and mtp-* files are companions, not loadable models.

    Offering them as models invites loading a projector as if it were a network —
    and skipping them entirely loses the memory and the vision capability they
    add to the model they hang off."""
    hub = tmp_path / "hub"
    snap = _hub_repo(hub, "org/Muse-GGUF", _REV_A, {
        "muse-IQ3_S.gguf": _KV,
        "mmproj-BF16.gguf": _KV,
        "mtp-muse-IQ3_S.gguf": _KV,
    })
    _fake_mmproj(snap / "mmproj-BF16.gguf")

    models, problems = mi.scan_cache(hub)
    assert problems == [], problems
    assert [m["id"] for m in models] == ["org/Muse-GGUF:IQ3_S"]
    assert models[0]["mmproj_bytes"] > 0
    assert models[0]["sidecar_type"] == "mmproj", "what the file says it is, not what we guessed"
    assert models[0]["has_vision"]


def test_two_quants_that_would_share_one_id_are_reported(tmp_path):
    """llama.cpp keeps the first file that yields an id and drops the rest.

    Inventing the second one as a model would put something on the page that no
    load can name; dropping it silently would hide two files answering to one
    name. So it is recorded."""
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Dup-GGUF", _REV_A, {
        "one/model-Q4_K_M.gguf": _KV,
        "two/model-Q4_K_M.gguf": _KV,
    })

    models, problems = mi.scan_cache(hub)
    assert [m["id"] for m in models] == ["org/Dup-GGUF:Q4_K_M"]
    assert [p["kind"] for p in problems] == ["duplicate-id"]


def test_an_incomplete_shard_set_in_the_cache_is_a_problem_not_a_row(tmp_path):
    """Half a sharded model cannot load, in the cache as much as in the library."""
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Half-GGUF", _REV_A, {
        "half-Q4_K_M-00002-of-00002.gguf": _KV,
    })

    models, problems = mi.scan_cache(hub)
    assert models == []
    assert [p["kind"] for p in problems] == ["incomplete-shards"]
    assert problems[0]["missing"] == [1]


def test_a_shared_id_goes_to_the_library_and_the_shadow_is_said(tmp_path):
    """Defensive branch, and honest about being defensive.

    A cache id always contains a slash and a library id is a directory name, which
    cannot — so today the two id spaces cannot collide. This is tested directly
    because the collision is not the point: `INSERT OR REPLACE` turns an unguarded
    collision into one model's row silently replacing another's, which surfaces as
    a wrong fit verdict on the models page and never as an error. The router's own
    cascade (server-models.cpp:702) says local beats cache, so the portal prices
    the same copy the runtime would load.
    """
    lib = ([{"id": "org/Muse-GGUF:Q4_K_M", "path": str(tmp_path / "lib.gguf"),
             "source": "library"}], [])
    cache = ([{"id": "org/Muse-GGUF:Q4_K_M", "path": str(tmp_path / "cache.gguf"),
               "source": "cache"},
              {"id": "org/Other-GGUF:Q4_K_M", "path": str(tmp_path / "other.gguf"),
               "source": "cache"}],
             [{"kind": "duplicate-id", "model": "org/Other-GGUF:Q4_K_M"}])

    models, problems = mi._merge_sources(lib, cache)
    assert [m["source"] for m in models] == ["library", "cache"]
    assert models[0]["path"].endswith("lib.gguf")
    kinds = sorted(p["kind"] for p in problems)
    assert kinds == ["duplicate-id", "shadowed-cache-model"], (
        "a merge that drops either side's problems makes the index look clean")


def test_rescan_says_which_models_came_from_where(tmp_path):
    """The library is a fence; the cache is not.

    A model under ~/ai/models/llama is there because the operator put it there.
    A model in the hub cache is loadable by the runtime whatever the portal lists,
    so a page that shows both identically overstates the control the portal has."""
    lib = tmp_path / "library"
    (lib / "Local-Model").mkdir(parents=True)
    _write_fake_gguf(lib / "Local-Model" / "Local-Model.gguf", _KV)
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Cached-GGUF", _REV_A, {"cached-Q4_K_M.gguf": _KV})

    summary = mi.rescan(models_dir=lib, cache_dir=hub, budget_bytes=40 * 2**30)
    assert summary["library"] == 1
    assert summary["cache"] == 1
    rows = {r["id"]: r["source"] for r in mi.rows()}
    assert rows == {"Local-Model": "library", "org/Cached-GGUF:Q4_K_M": "cache"}


def test_a_cache_model_is_marked_as_a_cache_model_on_the_page(monkeypatch):
    """The page says which rows the operator owns.

    Both sources are loadable and both take per-model settings, so this is not a
    claim about what can run. It is about provenance: a table that renders a
    hub-cache model exactly like a library model implies the portal put it there,
    and the operator who goes looking for it under the library directory finds
    nothing while the router keeps serving it.
    """
    from fastapi.testclient import TestClient

    from app import main as app_main

    hub = mi.paths.HOME / "fake-hub"
    _hub_repo(hub, "org/Cached-GGUF", _REV_A, {"cached-Q4_K_M.gguf": _KV})
    # Before the seed: _seed_library rescans, and the rescan is what has to see the
    # hub. Setting it afterwards would produce a page with no cache row and a test
    # that asserts nothing.
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    _seed_library(monkeypatch, ["Library-Model"])

    c = TestClient(app_main.create_app())
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]})
    body = c.get("/models").text
    assert "org/Cached-GGUF:Q4_K_M" in body, "a model the router can load is missing"
    assert "Library-Model" in body
    assert body.count("(hub cache)") == 1, (
        "the marker is on the cache row only — on both, or neither, and the "
        "operator cannot tell what the portal manages")


#: Which cache paths llama.cpp counts as models, measured against b11146 with
#: `--cache-list` over a fixture hub. The left column is the path relative to the
#: revision directory; the right is the id tag it earns, or None for "not a
#: model". Every row here was a live disagreement or a near miss:
#:   - case decides `-MTP-` vs `-mtp-`, and the portal used to fold them together
#:     (the runtime loads Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF:Q8_0; the portal
#:     refused to name it, so it could not be given a preset section at all);
#:   - the marker is a substring, so `mtpr` survives and `mtp-` does not;
#:   - `imatrix` was missing from the portal's list entirely;
#:   - the marker test covers the directory, the quant tag does not come from it.
_HF_NAME_TABLE = {
    "Qwopus3.5-4B-Coder-MTP-Q8_0.gguf":          "Q8_0",   # the real repo on this box
    "Model-4B-Coder-Mtp-Q8_0.gguf":              "Q8_0",   # mixed case is not a marker
    "model-Q4_K_M-mtpr.gguf":                    "MTPR",   # substring, not stem
    "model-Q4_K_M-mtp.gguf":                     "MTP",    # a draft, and still a model
    "model-Q8_0-mtp.gguf":                       "MTP",
    "model-coder-mtp-Q8_0.gguf":                 None,
    "model-mtp-Q4_K_M-MTP-Q8_0.gguf":            None,
    "mtp-Q8_0.gguf":                             None,
    "model-Q4_K_M-eagle3-Q8_0.gguf":             None,
    "model-Q4_K_M-dflash-Q8_0.gguf":             None,
    # An imatrix file is calibration metadata, not weights, so the portal will not
    # price it as a model. b11146 disagrees — it lists this as `repo:Q8_0` — and is
    # left out of the oracle fixture below for that one documented reason.
    "model-Q4_K_M-imatrix-Q8_0.gguf":            None,
    "model-mmproj-Q8_0.gguf":                    None,
    "model-Q8_0-mtpr-Q4_K_M.gguf":                "Q4_K_M",
    "Q6_K/model-Q6_K.gguf":                       "Q6_K",   # the usual per-quant layout
    "deep/nest-Q6_K.gguf":                        "Q6_K",
    "Q6_K/model.gguf":                            None,     # tag only in the directory
    "no_quant_here.gguf":                         None,
}

#: Shard sets that name a tag but are not whole. llama.cpp lists these; the portal
#: refuses to price a model that cannot load and reports it instead — the one
#: deliberate difference from the runtime, asserted below rather than smoothed over.
_HALF_SHARDS = ["IQ4_XS/half-IQ4_XS-00001-of-00002.gguf", "IQ4_XS/half-Q6_K-00001-of-00002.gguf"]


def test_which_cache_files_count_as_models(tmp_path):
    """The name rule, pinned without the binary.

    The oracle test below asks llama.cpp itself and so drifts with the installed
    build; this table is the portal's own opinion written down, including the
    cases that only exist because someone named a repo in mixed case.
    """
    hub = tmp_path / "hub"
    for i, (rel, tag) in enumerate(_HF_NAME_TABLE.items()):
        _hub_repo(hub, f"org/N{i:02d}-GGUF", _REV_A, {rel: _KV})
    got = {row["id"] for row in mi.scan_cache(hub)[0]}
    want = {f"org/N{i:02d}-GGUF:{tag}" for i, (rel, tag) in enumerate(_HF_NAME_TABLE.items())
            if tag is not None}
    assert got == want, "\n".join(
        [f"  extra:   {x}" for x in sorted(got - want)]
        + [f"  missing: {x}" for x in sorted(want - got)])


def test_an_incomplete_shard_set_is_named_the_way_the_router_names_it(tmp_path):
    """A half-downloaded set is a problem with the router's id on it.

    llama.cpp lists these (part 1 carries the header, so it has something to
    name), which is why a user can see a model in the router's list that the
    portal never showed them. The portal still refuses to price it — the byte
    total is a fraction of the real one and a load would fail — but it has to
    say which id that is, in the router's own spelling.
    """
    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Half-GGUF", _REV_A, {rel: _KV for rel in _HALF_SHARDS})
    models, problems = mi.scan_cache(hub)
    assert models == []
    named = {p["id"] for p in problems if p["kind"] == "incomplete-shards"}
    assert named == {"org/Half-GGUF:IQ4_XS", "org/Half-GGUF:Q6_K"}, problems


def test_the_portal_and_the_runtime_agree_on_what_the_cache_holds(tmp_path):
    """The contract that keeps the transcribed rules honest.

    Asked directly — `llama-server --cache-list` is the runtime reading the same
    directory with its own code. If these two sets ever disagree, the portal is
    naming models the router will not, and every per-model setting written against
    that name is inert.

    Skipped where llama-server is not installed; AI_LLAMA_SERVER names it
    explicitly when the binary is not on PATH."""
    import shutil
    import subprocess

    binary = os.environ.get("AI_LLAMA_SERVER") or shutil.which("llama-server")
    if not binary:
        pytest.skip("no llama-server to adjudicate the cache listing")

    hub = tmp_path / "hub"
    _hub_repo(hub, "org/Muse-GGUF", _REV_A, {
        "IQ3_S/muse-IQ3_S-00001-of-00002.gguf": _KV,
        "IQ3_S/muse-IQ3_S-00002-of-00002.gguf": _KV,
        "mmproj-BF16.gguf": {"general.type": "mmproj"},
        "mtp-muse-IQ3_S.gguf": _KV,
    })
    _hub_repo(hub, "org/Odd-GGUF", _REV_B, {
        "plain-q4_k_m.gguf": _KV, "other.F16.gguf": _KV, "no_quant_here.gguf": _KV,
    })
    _hub_repo(hub, "org/Stale-GGUF", _REV_A, {"old-Q8_0.gguf": _KV}, refs=None)
    # Every shape from the table above, so a runtime that changes its mind about
    # one of them fails here instead of silently naming models the portal will
    # never offer (or hiding ones the router can load).
    _hub_repo(hub, "org/Shapes-GGUF", _REV_B, {
        rel: _KV for rel, tag in _HF_NAME_TABLE.items()
        if tag is not None and not rel.endswith("-of-00002.gguf")
    })
    _hub_repo(hub, "org/Drafts-GGUF", _REV_B, {
        rel: _KV for rel, tag in _HF_NAME_TABLE.items()
        if tag is None and "imatrix" not in rel  # the runtime lists it; see the table
    })
    _hub_repo(hub, "org/Half-GGUF", _REV_B, {rel: _KV for rel in _HALF_SHARDS})

    env = {**os.environ, "HF_HUB_CACHE": str(hub)}
    try:
        out = subprocess.run([binary, "--cache-list"], env=env, capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"could not run {binary}: {e}")
    # "   3. org/Muse-GGUF:IQ3_S" — and nothing else. llama.cpp also logs with a
    # dotted timestamp, so the pattern has to be the numbering, not "starts with a
    # digit".
    listed = {m.group(1)
              for m in (re.match(r"^\s*\d+\.\s+(\S+)\s*$", line)
                        for line in (out.stdout + out.stderr).splitlines())
              if m}
    if not listed and out.returncode != 0:
        # An older runtime has no --cache-list to ask. There is no answer to
        # compare against, and failing here would be a statement about the
        # installed build, not about the naming rules.
        pytest.skip(f"{binary} does not list the cache itself "
                    f"(exit {out.returncode}): {out.stderr[:200]!r}")
    assert listed, f"{binary} --cache-list listed nothing:\n{out.stdout[:400]}\n{out.stderr[:400]}"

    mine = {m["id"] for m in mi.scan_cache(hub)[0]}
    _, problems = mi.scan_cache(hub)
    halves = {p["id"] for p in problems if p["kind"] == "incomplete-shards"}
    # The one allowed difference, and it has to go both ways: the runtime names
    # half-downloaded sets and the portal reports them instead of pricing them.
    assert halves and halves <= listed, (
        f"expected the runtime to list the incomplete sets {sorted(halves)}; "
        f"it listed {sorted(listed)}")
    assert mine == listed - halves, (
        f"portal says {sorted(mine)}, llama.cpp says {sorted(listed - halves)}")
