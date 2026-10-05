"""Predicting the context a client gets — checked against what llama.cpp did on this box.

`modelindex.context_window()` answers "how much context will a client be given for this model,
and who decided it". It is a predictor, so it is pinned to measurements, not to reasoning. The
runs below ran on this box against b11396 with `Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0`
(4.29 GiB, trained 262144) on a loopback port with no presets; the numbers are the
`default_generation_settings.n_ctx` and `/slots` values the server itself reported.
docs/PORTAL_PLAN.md §6.12.20 carries the table and the source lines.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-cw-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import modelindex as mi  # noqa: E402

GIB = 1 << 30
TRAINED = 262144
WEIGHTS = 4_610_579_904          # the probe model's real file size
INPUTS = {"block_count": 48, "attention_count": 24, "attention_kv_count": 2,
          "embedding_length": 2560, "full_attention_interval": 4, "ctx": TRAINED}


def row(trained=TRAINED, weights=WEIGHTS, priceable=True):
    """A stored index row the estimator can reprice (est_kv must match its own inputs)."""
    kv = mi.kv_bytes_at(INPUTS, trained)
    r = {"id": "probe/model:Q8_0", "ctx_trained": trained, "est_weights": weights,
         "est_ctx": trained, "est_kv": kv, "layers": INPUTS["block_count"],
         "kv_heads": INPUTS["attention_kv_count"], "attention_heads": INPUTS["attention_count"],
         "embedding_length": INPUTS["embedding_length"],
         "full_attention_interval": INPUTS["full_attention_interval"]}
    if priceable:
        r["est_inputs"] = dict(INPUTS)
    return r


def cw(settings, budget=200 * GIB, **kw):
    return mi.context_window(row(**kw), settings, budget_bytes=budget)


# ------------------------------------------------------------- the measured table
def test_auto_slots_leave_the_window_at_the_trained_context():
    """Measured: no settings, 4 auto slots -> window 262144, kv_unified true."""
    r = cw({})
    assert r["slots"] == 4 and r["kv_unified"] is True
    assert r["window"] == TRAINED
    assert r["reduced"] is False
    assert "trained context" in r["by"]


def test_naming_slots_with_a_pinned_context_divides_it():
    """Measured: --parallel 2 -c 32768 -> window 16384, with the server's own warning."""
    r = cw({"parallel": 2, "ctx-size": 32768})
    assert r["kv_unified"] is False
    assert r["window"] == 32768 // 2
    assert r["reduced"] is True
    assert any("divides" in n or "divided" in n for n in r["notes"])


def test_naming_slots_and_unifying_keeps_the_whole_pinned_pool():
    """Measured: --parallel 2 -c 32768 --kv-unified -> window 32768 (shared, not halved)."""
    r = cw({"parallel": 2, "ctx-size": 32768, "kv-unified": "on"})
    assert r["window"] == 32768
    assert r["kv_unified"] is True


def test_unset_context_with_named_slots_extends_the_pool_to_cover_them():
    """Measured: --parallel 2, no -c -> server uses 524288; window stays 262144."""
    r = cw({"parallel": 2})
    assert r["pool_want"] == TRAINED * 2
    assert r["window"] == TRAINED


def test_kv_unified_per_slot_names_the_window_exactly():
    """Measured: --kv-unified-per-slot 8192 at 4 auto slots -> window 8192, pool 32768."""
    r = cw({"kv-unified-per-slot": 8192})
    assert r["window"] == 8192
    assert r["pool_want"] == 8192 * 4
    assert "kv-unified-per-slot" in r["by"]


# ------------------------------------------------------------- the silent shrink
def _mid_budget(r=None):
    """A budget that fits one token and not the whole context -- the shape of a busy box.

    The shrink window is narrow and model-specific: between weights + fixed headroom + one token's
    cache, and weights + the full cache + headroom. Derived from the row's own price, so the test
    does not hard-code this geometry's numbers. For the probe model that band is 8.29 GiB to
    10.78 GiB; for a 78 GiB model it is 82.0 GiB to 84.5 GiB.
    """
    r = r if r is not None else row()
    floor = mi.peak_bytes(r, 1)
    full = mi.peak_bytes(r, TRAINED)
    assert floor is not None and full is not None and full > floor, (floor, full)
    return floor + (full - floor) // 2


def test_a_tight_aperture_shrinks_the_window_when_ctx_size_is_unset():
    """The report this exists for: 'half the model's context', with no setting set.

    The trained context costs more than the aperture holds, so fit takes a smaller pool. The
    portal has to see that coming, because the router's own log does not show it.
    """
    wide = cw({}, budget=200 * GIB)
    tight = cw({}, budget=_mid_budget())
    assert wide["window"] == TRAINED
    assert tight["window"] is not None and tight["window"] < TRAINED
    assert tight["reduced"] is True
    assert tight["by"].startswith("fit")
    assert any("trace level" in n for n in tight["notes"])


def test_writing_ctx_size_takes_the_decision_back_from_fit():
    """Measured: a pinned ctx-size locks fit out; the window is what you wrote."""
    r = cw({"ctx-size": 131072}, budget=42 * GIB)
    assert r["window"] == 131072
    assert "ctx-size" in r["by"]
    assert "fit is locked out" in r["by"]


def test_a_busy_box_is_predicted_to_shrink_the_heaviest_resident():
    """The case the operator hit: a 78 GiB model on a box whose aperture cannot pay for its cache."""
    heavy = row(weights=int(78.0 * GIB))
    r = mi.context_window(heavy, {}, budget_bytes=_mid_budget(heavy))
    assert r["window"] is not None and r["window"] < TRAINED, r
    assert r["by"].startswith("fit")


def test_a_model_whose_weights_exceed_the_budget_is_said_not_to_fit():
    """Not a smaller window -- no context at all. Say it, and do not crash taking the minimum."""
    heavy = row(weights=int(78.0 * GIB))
    r = mi.context_window(heavy, {}, budget_bytes=40 * GIB)
    assert r["window"] is None
    assert r["pool"] == 0
    assert "does not fit" in r["by"]


# ------------------------------------------------------------- honesty limits
def test_a_row_that_cannot_be_repriced_is_reported_as_unpredictable():
    """No est_inputs: the stored cache prices its own context, and nothing else.

    A pinned ctx-size asks for a context the row never priced, so the honest answer is "cannot
    predict" -- not the trained-context number, which is what an optimistic reader would infer.
    """
    r = cw({"ctx-size": 65536}, priceable=False)
    assert r["window"] is None
    assert r["by"] == "unpriced"
    assert any("cannot price" in n for n in r["notes"])


def test_a_header_with_no_trained_context_says_so():
    r = cw({"ctx-size": 0}, trained=0)
    assert r["window"] is None and r["by"] == "unpriced"


def test_a_pinned_context_below_trained_is_called_reduced_and_named():
    r = cw({"ctx-size": 8192})
    assert r["reduced"] is True
    assert r["window"] == 8192
    assert any("below the model's trained" in n for n in r["notes"])


def test_the_auto_slot_count_matches_what_the_server_resolves_to():
    """4 is not a taste: it is what 'auto' becomes (server.cpp:161-166)."""
    assert mi.AUTO_PARALLEL_SLOTS == 4
    assert cw({})["slots"] == 4
    assert cw({"parallel": -1})["slots"] == 4


def test_junk_from_a_header_becomes_unpriced_not_a_crash():
    """Header numbers are attacker-shaped; a str must not become a TypeError from a page."""
    r = mi.context_window({"ctx_trained": "wide", "est_weights": 1},
                          {"parallel": "two", "ctx-size": "lots"}, budget_bytes=10 * GIB)
    assert r["window"] is None and r["by"] == "unpriced"


# ------------------------------------------------------------------- the 2026-10-05 sweep
#
# The operator ran eight setting combinations against lmstudio-community/
# codegemma-2b-GGUF:Q5_K_M (1.71 GiB, trained 8192) on b11396, and read the window back
# from the server's own /props. Every combination reported 8192 — including pools of 32768
# and 131072. The trained context is the cap: the server hands a slot
# min(n_ctx_seq, kv-unified-per-slot, n_ctx_train), so a bigger pool buys slots, not tokens.
# The predictor must reproduce all eight rows; rows 2, 3, 6 and 8 are the ones a predictor
# without the trained-context cap gets wrong.
# docs/PORTAL_PLAN.md sec 6.12.22 carries the run.

CG_INPUTS = {"block_count": 18, "attention_count": 8, "attention_kv_count": 1,
             "embedding_length": 2048}
CG_TRAINED = 8192
CG_WEIGHTS = 1_835_008_000                       # 1.71 GiB, measured from the file


def cg_row():
    kv = mi.kv_bytes_at(CG_INPUTS, CG_TRAINED)
    return {"id": "lmstudio-community/codegemma-2b-GGUF:Q5_K_M", "ctx_trained": CG_TRAINED,
            "est_ctx": CG_TRAINED, "est_weights": CG_WEIGHTS, "est_kv": kv,
            "layers": CG_INPUTS["block_count"], "kv_heads": CG_INPUTS["attention_kv_count"],
            "attention_heads": CG_INPUTS["attention_count"],
            "embedding_length": CG_INPUTS["embedding_length"], "est_inputs": dict(CG_INPUTS)}


# Each row is what the operator typed on the command line, plus the slots and window the
# server reported. `parallel` appears only where it was typed: a row that reports 4 slots
# with no `--parallel` got them from auto, and auto is what keeps KV unified.

MEASURED_2026_10_05 = [
    ({}, 4, 8192),
    ({"parallel": 2, "ctx-size": 32768}, 2, 8192),
    ({"parallel": 2, "ctx-size": 32768, "kv-unified": "on"}, 2, 8192),
    ({"parallel": 2}, 2, 8192),
    ({"kv-unified-per-slot": 8192}, 4, 8192),
    ({"parallel": 2, "kv-unified-per-slot": 8192}, 2, 8192),
    ({"ctx-size": 8192}, 4, 8192),
    ({"parallel": 4, "ctx-size": 131072}, 4, 8192),
]


def test_the_predictor_reproduces_every_row_of_the_sweep():
    """Every row capped at 8192, in pools from 8192 to 131072.

    Drop the trained-context cap and row 2 predicts 16384 and row 8 predicts 32768.
    Drop the auto/unified rule and the two rows that typed no `--parallel` predict 2048.
    Both are numbers the server reported, not reasoning.
    """
    for settings, slots, window in MEASURED_2026_10_05:
        pred = mi.context_window(cg_row(), dict(settings), budget_bytes=100 * GIB)
        assert pred["slots"] == slots, (settings, pred["slots"], pred["slots_named"])
        assert pred["window"] == window, (settings, pred["window"], pred["by"])
        assert pred["reduced"] is False, (settings, pred["window"], pred["trained"])


def test_the_sweep_rows_that_divide_say_who_divided_them():
    """Rows 2, 6 and 8 name the settings that split the pool.

    The window number alone cannot tell an operator why it halved. The note does, and it
    only exists when slots are named with ctx-size written.
    """
    for settings in ({"parallel": 2, "ctx-size": 32768}, {"parallel": 4, "ctx-size": 131072}):
        pred = mi.context_window(cg_row(), settings, budget_bytes=100 * GIB)
        hits = [n for n in pred["notes"] if "naming slots turns the unified pool off" in n]
        assert len(hits) == 1, (settings, pred["notes"])
