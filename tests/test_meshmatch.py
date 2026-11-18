"""meshmatch: the deterministic model-equivalence rubric (owner 2026-11-17).
Injected cards only: the suite never touches live doors. The rubric's law
is the ORDER (exact -> ctx -> params(bias big) -> weights(bias big) -> fit
-> tags only on ties) and that every drop is STATED, never silent."""
from app.core import meshmatch as mm


def _cand(node, service, ctx=32768, params_b=27.0, active_b=None,
          weights_gib=16.0, kv_gib=2.0, tags=None):
    return {"node": node, "service": service, "ctx": ctx,
            "params": {"params_b": params_b,
                       "active_b": active_b if active_b is not None else params_b,
                       "params_src": "header-geometry"},
            "weights_bytes": int(weights_gib * 2**30),
            "kv_bytes": int(kv_gib * 2**30),
            "tags": set(tags or ())}


def test_exact_match_wins_outright():
    cands = [_cand("halo", "ThinkingCap-Qwen3.8-27B-Q4_K_M"),
             _cand("forge", "Some-Other-27B", tags={"roleplay"})]
    ranked, filtered = mm.rank({"model": "ThinkingCap-Qwen3.8-27B-Q4_K_M.gguf",
                                "ctx": 16384}, cands)
    assert ranked[0]["service"].startswith("ThinkingCap")
    assert "EXACT model match" in ranked[0]["why"][0]


def test_exact_match_ignores_quant_and_path_noise():
    cands = [_cand("halo", "roleplay-Xing4.0-29B-Q5_K_M")]
    ranked, _ = mm.rank({"model": "/home/cortana/ai/models/llama/roleplay-Xing4.0-29B-GGUF/"
                                 "roleplay-Xing4.0-29B-Q5_K_M.gguf"}, cands)
    assert "EXACT model match" in ranked[0]["why"][0]


def test_smaller_context_is_filtered_with_a_stated_reason():
    cands = [_cand("halo", "BigModel", ctx=16384), _cand("forge", "WiderModel", ctx=131072)]
    ranked, filtered = mm.rank({"model": "Target", "ctx": 32768}, cands)
    assert [r["service"] for r in ranked] == ["WiderModel"]
    assert filtered[0]["service"] == "BigModel" and "ctx 16384 < 32768" in filtered[0]["why"]


def test_params_bias_prefers_bigger_that_is_closer():
    # owner rubric: closer wins; a 10% overshoot is cheaper than a 10% undershoot
    cands = [_cand("a", "NearSmall", params_b=24.3),   # -10%
             _cand("b", "NearBig", params_b=29.7)]     # +10%
    ranked, _ = mm.rank({"model": "Target", "params_b": 27.0}, cands)
    assert ranked[0]["service"] == "NearBig"
    assert "params 29.7B vs 27.0B" in ranked[0]["why"]


def test_far_bigger_loses_to_near_smaller():
    cands = [_cand("a", "NearSmall", params_b=25.0),
             _cand("b", "Huge", params_b=70.0)]
    ranked, _ = mm.rank({"model": "Target", "params_b": 27.0}, cands)
    assert ranked[0]["service"] == "NearSmall"


def test_weights_bytes_is_the_second_chain_and_quant_never_names():
    # no params signal at all: the file-size proxy decides, bias bigger.
    # quant labels are NOT a rubric input (owner law): bytes are physics.
    a = _cand("a", "ModelA", params_b=None, weights_gib=14.0, tags={"quant:Q4_K_M"})
    b = _cand("b", "ModelB", params_b=None, weights_gib=17.0, tags={"quant:IQ3_S"})
    for c in (a, b):
        c["params"] = None
    a["params"] = b["params"] = None
    ranked, _ = mm.rank({"model": "Target", "weights_bytes": 16 * 2**30}, [a, b])
    assert ranked[0]["service"] == "ModelB"  # 17 GiB is closer to 16 than 14, bias big


def test_fit_filter_drops_oversized_and_keeps_bigger_that_fits():
    cands = [_cand("small-node", "FitsMe", weights_gib=10.0, kv_gib=1.0),
             _cand("small-node", "TooBig", weights_gib=40.0, kv_gib=6.0)]
    ranked, filtered = mm.rank({"model": "Target", "ctx": 8192,
                                "weights_bytes": 20 * 2**30, "free_ram_mib": 24576}, cands)
    assert [r["service"] for r in ranked] == ["FitsMe"]
    assert "fit" in filtered[0]["why"]


def test_tags_only_break_ties_never_outrank_size():
    # identical size signal: tags decide
    a = _cand("a", "PlainTwin", params_b=27.0, tags={"code"})
    b = _cand("b", "RoleTwin", params_b=27.0, tags={"roleplay"})
    ranked, _ = mm.rank({"model": "Target", "params_b": 27.0, "tags": ["roleplay"]}, [a, b])
    assert ranked[0]["service"] == "RoleTwin"
    # a FAR size miss is not outranked by a tag hit
    near = _cand("a", "NearNoTag", params_b=27.5, tags=set())
    far = _cand("b", "FarTagged", params_b=60.0, tags={"roleplay"})
    ranked, _ = mm.rank({"model": "Target", "params_b": 27.0, "tags": ["roleplay"]}, [near, far])
    assert ranked[0]["service"] == "NearNoTag"


def test_moe_scored_on_active_parameters():
    # Qwen3-Coder-30B-A3B: 30B total, ~3B active -- latency physics (owner MoE tag)
    moe = _cand("halo", "Qwen3-Coder-30B-A3B", params_b=30.0, active_b=3.0,
                tags={"moe", "code"})
    dense = _cand("forge", "Dense7B", params_b=7.0, tags={"code"})
    ranked, _ = mm.rank({"model": "Target", "params_b": 7.0, "tags": ["code"]}, [moe, dense])
    assert ranked[0]["service"] == "Dense7B"  # active 3B vs 7B: dense is closer


def test_params_est_dense_geometry():
    meta = {"block_count": 64, "embedding_length": 5120, "attention_count": 40,
            "attention_kv_count": 8, "rope_dimension_count": 128,
            "feed_forward_length": 27520, "vocab_size": 151936}
    p = mm.params_est(meta)
    # per layer: attn 5120*(5120+2*1024)+5120*5120 = 5120*7168+5120*5120
    attn = 5120 * (40 * 128 + 2 * 8 * 128) + 40 * 128 * 5120
    ffn = 3 * 5120 * 27520
    total = 64 * (attn + ffn) + 2 * 151936 * 5120
    assert abs(p["params_b"] - total / 1e9) / (total / 1e9) < 0.01
    assert p["params_src"] == "header-geometry"


def test_params_est_refuses_incomplete_geometry():
    assert mm.params_est(None) is None
    assert mm.params_est({"block_count": 32}) is None
    assert mm.params_est({"block_count": 32, "embedding_length": 4096,
                          "rope_dimension_count": 128, "attention_count": 32}) is None
    # (no feed_forward_length: cannot price the FFN -> None, not a guess)


def test_tags_from_name_and_geometry_are_deterministic():
    t = mm.tags_from("ThinkingCap-Qwen3.8-27B-RCO-4.2bpw", {"expert_count": 0, "filetype": "Q4_K_M"})
    assert "efficient_thinking" in t and "family:qwen3.8" in t and "quant:Q4_K_M" in t
    t2 = mm.tags_from("Qwen3-Coder-30B-A3B-Instruct", {"expert_count": 128})
    assert "moe" in t2 and "code" in t2
    assert t == mm.tags_from("thinkingcap-qwen3.8-27b-rco-4.2bpw",
                             {"expert_count": 0, "filetype": "Q4_K_M"})


def test_candidates_from_meshd_card_shape():
    card = {"node": "halo", "kind": "llm-router", "service": "ThinkingCap-Qwen3.8-27B-Q4_K_M",
            "state": "catalog",
            "limits": {"context": 262144},
            "requires": {"ram_gb_min": 34, "backend": "llama-cuda-cpu"},
            "measured": {"weights_bytes": 16 * 2**30, "kv_bytes": 2**30,
                         "gguf": {"block_count": 64, "embedding_length": 5120,
                                  "attention_count": 40, "attention_kv_count": 8,
                                  "rope_dimension_count": 128,
                                  "feed_forward_length": 27520, "vocab_size": 151936,
                                  "filetype": "Q4_K_M"}}}
    cands = mm.candidates_from_cards([card])
    assert cands[0]["ctx"] == 262144
    assert cands[0]["params"]["params_b"] > 20
    assert "family:qwen3.8" in cands[0]["tags"]
    ranked, _ = mm.rank({"model": "Whatever", "params_b": 27.0, "ctx": 32768}, cands)
    assert ranked[0]["node"] == "halo"
