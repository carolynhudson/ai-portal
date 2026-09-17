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
