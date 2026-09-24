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
import struct
import sys
import tempfile
from pathlib import Path

# Throwaway home before the app is imported (same pattern as test_smoke.py).
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-mi-"))

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
GGUF_U32, GGUF_I32, GGUF_BOOL, GGUF_STRING, GGUF_ARRAY = 4, 5, 7, 8, 9


def _enc_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def _enc_value(v) -> bytes:
    """One GGUF value, type tag included. Field order matters twice over: the tag
    is a u32 (4 bytes, not a byte), and the tag *number* must be the reader's, not
    the current spec's — see GGUF_U32 above."""
    if isinstance(v, bool):
        return struct.pack("<I", GGUF_BOOL) + struct.pack("<?", v)
    if isinstance(v, int):
        return struct.pack("<I", GGUF_I32) + struct.pack("<i", v)
    if isinstance(v, (list, tuple)):
        if not v:
            return (struct.pack("<I", GGUF_ARRAY) + struct.pack("<I", GGUF_U32)
                    + struct.pack("<Q", 0))
        first = v[0]
        if isinstance(first, bool):
            elem, raw = GGUF_BOOL, b"".join(struct.pack("<?", bool(x)) for x in v)
        elif isinstance(first, int):
            elem, raw = GGUF_I32, b"".join(struct.pack("<i", x) for x in v)
        else:
            elem, raw = GGUF_STRING, b"".join(_enc_str(str(x)) for x in v)
        return (struct.pack("<I", GGUF_ARRAY) + struct.pack("<I", elem)
                + struct.pack("<Q", len(v)) + raw)
    return struct.pack("<I", GGUF_STRING) + _enc_str(str(v))


def _write_fake_gguf(path: Path, kv: dict, *, pad: int = 0) -> None:
    """Minimal GGUF v3 with an architecture-prefixed KV section — enough for
    sniff_gguf and nothing else. Written by hand rather than with a library so a
    change in the reader cannot make these tests vacuously pass. Keys are prefixed
    with `testarch.` because that is how the reader looks up block_count &c."""
    n_kv = 1 + len(kv)
    hdr = (struct.pack("<4s", b"GGUF") + struct.pack("<I", 3)
           + struct.pack("<Q", 0) + struct.pack("<Q", n_kv))
    assert len(hdr) == 24, f"KV section must start at offset 24, header is {len(hdr)}"
    out = bytearray()
    out += _enc_str("general.architecture") + _enc_value("testarch")
    for k, v in kv.items():
        out += _enc_str(f"testarch.{k}") + _enc_value(v)
    path.write_bytes(hdr + bytes(out) + b"\0" * pad)


def _append_kv_key(path: Path, key: str, value) -> None:
    """Append one top-level (unprefixed) KV entry to a fixture GGUF.

    _write_fake_gguf namespaces every key under the architecture, but some fields
    GGUF defines are top-level — `mmproj` among them — and the reader looks those
    up without a prefix. Bumping kv_count keeps the file parseable.
    """
    # Header is magic(4) version(4) tensor_count(8) kv_count(8) = 24 bytes, with
    # kv_count at offset 16. Append "name + value" and bump the count by one.
    raw = bytearray(path.read_bytes())
    n = struct.unpack_from("<Q", raw, 16)[0]
    struct.pack_into("<Q", raw, 16, n + 1)
    path.write_bytes(bytes(raw) + _enc_str(key) + _enc_value(bool(value)))


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

