"""The model-name reducer: the owner's example is the spec, collisions fail.

(2026-11-02; measurement for the phone layout work, not a deployed display:
nothing in the UI imports `modelshort` yet.)
"""
from app.core.modelshort import reduce_names, reduce_one

OWNER_EXAMPLE = "ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S"
OWNER_ANSWER = "Swift-Qwen3.8-FN:IQ3_S"

# a realistic corner of this library: the same base in several quants, a
# sibling family, and a family with a version that is NOT shared.
LIBRARY = [
    OWNER_EXAMPLE,
    "ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_XXS",
    "ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q4_K_M",
    "ukisai/Swift-2.0-Qwen3.8-Flash-Next-GGUF:IQ4_XS",
    "bottlecapai/ThinkingCap-Qwen3.5-9B-ABCD-GGUF:Q5_K_M",
    "gguf-indexer/tinyllama-1.1B-chat-GGUF:Q2_K",
]


def test_the_owners_exact_reduction():
    # the owner's answer is a corpus judgement: 1.5/GSQ/RCO are redundant
    # because the family carries them, so the reduction is asked of the
    # family (three quants of one model), as in the real library.
    fam = [OWNER_EXAMPLE,
           OWNER_EXAMPLE.replace("IQ3_S", "IQ3_XXS"),
           OWNER_EXAMPLE.replace("IQ3_S", "Q4_K_M")]
    assert reduce_names(fam)[OWNER_EXAMPLE] == OWNER_ANSWER
    # in isolation the conservative form: formats and abbreviations only
    assert reduce_one(OWNER_EXAMPLE) == "Swift-1.5-Qwen3.8-FN-GSQ-RCO:IQ3_S"


def test_the_quant_is_always_kept():
    short = reduce_names([OWNER_EXAMPLE,
                          OWNER_EXAMPLE.replace("IQ3_S", "IQ3_XXS")])
    assert sorted(short.values()) == ["Swift-Qwen3.8-FN:IQ3_S",
                                       "Swift-Qwen3.8-FN:IQ3_XXS"]


def test_the_whole_library_reduces_without_collisions():
    short = reduce_names(LIBRARY)
    assert len(set(short.values())) == len(LIBRARY), \
        f"collisions: {short}"
    # with a Swift-2.0 sibling in the group, `1.5` and `GSQ/RCO` are NOT
    # group-shared and stay: redundancy is judged against the group that
    # exists, not a wish. (The owner's real library, where every Swift model
    # carries 1.5 and GSQ/RCO, reduces to the owner's answer - pinned by
    # test_a_version_shared_by_the_whole_group_is_redundant.)
    assert short[OWNER_EXAMPLE] == "Swift-1.5-Qwen3.8-FN-GSQ-RCO:IQ3_S"
    s20 = short["ukisai/Swift-2.0-Qwen3.8-Flash-Next-GGUF:IQ4_XS"]
    assert s20 == "Swift-2.0-Qwen3.8-FN:IQ4_XS", \
        "a version NOT shared in the group is not redundant"


def test_a_version_shared_by_the_whole_group_is_redundant():
    # the owner's actual library: every Swift model here is 1.5.
    fam = [OWNER_EXAMPLE,
           OWNER_EXAMPLE.replace("IQ3_S", "IQ3_XXS"),
           OWNER_EXAMPLE.replace("IQ3_S", "Q4_K_M")]
    short = reduce_names(fam)
    assert short[OWNER_EXAMPLE] == OWNER_ANSWER


def test_a_collision_re_admits_distinguishing_tokens():
    # "Foo-XX" and "Foo" collapse under every rule; the collision law must
    # re-admit XX for one of them.
    short = reduce_names(["org/Foo-XX:q", "org/Foo:q"])
    assert len(set(short.values())) == 2, short
    assert short["org/Foo-XX:q"] == "Foo-XX:q"


def test_quant_methods_are_not_format_tags():
    # AWQ/GPTQ distinguish quant families: they survive, GGUF does not.
    s = reduce_one("org/Model-Llama3.1-AWQ:q")
    assert "AWQ" in s and "GGUF" not in reduce_one("org/Model-Llama3.1-GGUF:q")


def test_junk_is_not_a_crash():
    assert reduce_one("") == ""
    assert reduce_one(None) == ""
    assert reduce_names([]) == {}
    assert reduce_one("nocolons-nodash") == "nocolons-nodash"
    # an only member loses nothing: compression is sharedness, and a name
    # shares with nobody. (Live-library finding: "L3-8B-Stheno-v3.2".)
    assert reduce_one("L3-8B-Stheno-v3.2") == "L3-8B-Stheno-v3.2"


def test_mesh_repo_tag_shape_keeps_its_tag():
    # repo:TAG names (the model index's hub-cache shape): the tag is the
    # distinguisher, so it survives as the quant.
    assert reduce_one("org/Repo:Gemma-2b-Q4") == "Repo:Gemma-2b-Q4"
