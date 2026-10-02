"""The capability columns: can this file autocomplete, draft, or rank?

Three questions a personality has to answer before it picks a model — is it
usable for autocomplete, does it carry its own speculative-decoding head, can it
rerank — and all three are answered out of the file rather than guessed from a
repository name. These tests pin the answer for each shape that occurs, plus the
two shapes where an honest index has to say "unknown" or "these disagree".

docs/MODEL_SIGNALS.md carries the measurements these rules were built from.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.core import modelindex as mi, modelmeta  # noqa: E402
from gguf_fixture import (_gguf_with_tensors, _write_fake_gguf,  # noqa: E402
                          _write_raw_gguf)

def _special(name: str) -> str:
    """The ASCII special token `<|name|>`, built from pieces.

    Written this way on purpose: the literal form of these tokens does not
    survive every path into this file. The first version of this fixture said
    `FIM = ["", "", ""]` — three empty strings, because the bars were eaten
    somewhere upstream — and the FIM tests then failed for a reason that had
    nothing to do with the rule under test. Concatenation cannot be mangled.
    """
    return "<" + "|" + name + "|" + ">"


FIM = [_special("fim_prefix"), _special("fim_middle"), _special("fim_suffix")]


def _one(tmp_path: Path, name: str, writer) -> dict:
    """Scan a library holding exactly one model and return its row."""
    folder = tmp_path / name
    folder.mkdir(parents=True)
    writer(folder / f"{name}.gguf")
    models, problems = mi.scan_library(tmp_path)
    assert not problems, problems
    assert len(models) == 1
    return models[0]


# ---------------------------------------------------------------- autocomplete

def test_the_fim_fixture_actually_contains_tokens():
    """Guard on the fixtures themselves.

    Every FIM rule below reads these strings. When the literal form arrived as
    empty strings the capability tests failed as though the reader were broken,
    and the diagnosis cost an hour. One assertion says out loud which side is
    at fault if the fixture ever goes blank again.
    """
    for t in FIM:
        assert len(t) > 6 and t.startswith("<") and t.endswith(">"), \
            f"fixture token degraded to {t!r} — the test data is broken, not the rule"


def test_a_fim_bearing_vocab_is_indexed_as_autocomplete_capable(tmp_path):
    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.added_tokens", FIM[:1]),
                            ("tokenizer.ggml.tokens", ["hello", "world"] + FIM)])
    row = _one(tmp_path, "fim-model", write)
    assert row["has_fim"] is True
    assert row["vocab_size"] == 5


def test_a_large_vocabulary_is_still_searched_not_summarised_away(tmp_path):
    """The regression this whole column exists to prevent.

    A GGUF carries its whole vocabulary in the metadata head, and the reader used
    to summarise any array over 64 entries as the placeholder string
    "<250002 strings skipped>". Consequences, both silent: `len()` of that string
    reported a 24-token vocabulary, and no amount of searching a 24-character
    placeholder finds the FIM specials. A real model's vocab is always over the
    skip threshold, so a capability rule built on it was dead on arrival.
    """
    toks = [f"tok{i}" for i in range(300)]
    toks[123] = _special("fim_middle")

    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.tokens", toks)])
    row = _one(tmp_path, "big-vocab", write)
    assert row["vocab_size"] == 300, "the array was skipped, not read"
    assert row["has_fim"] is True


def test_a_vocab_without_fim_specials_indexes_as_false_not_unknown(tmp_path):
    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.tokens", ["hello", "world", "<s>"])])
    row = _one(tmp_path, "no-fim", write)
    assert row["has_fim"] is False, "'checked and absent' is not the same as 'unaskable'"


def test_an_unreadable_vocab_leaves_autocomplete_capability_unknown(tmp_path):
    """None, not False. A UI that shows "cannot autocomplete" for a file whose
    vocabulary we could not read would hide a working slot behind a parse gap."""
    def write(p):
        _write_fake_gguf(p, {"block_count": 4})
    row = _one(tmp_path, "no-tokenizer", write)
    assert row["has_fim"] is None
    assert row["vocab_size"] is None


def test_a_fullwidth_fim_marker_counts(tmp_path):
    """deepseek2's tokenizer spells its specials with fullwidth bars:
    <｜fim▁begin｜>. A rule that matched only ASCII pipes would call a working
    autocomplete model incapable."""
    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.tokens",
                             ["<｜fim▁begin｜>", "<｜fim▁hole｜>", "<｜fim▁end｜>", "hi"])])
    assert _one(tmp_path, "fullwidth", write)["has_fim"] is True


def test_an_ordinary_token_containing_fim_is_not_a_fim_special(tmp_path):
    """The shape rule, not a bare substring: a BPE piece that happens to spell
    "fim" inside a word must not make a model look autocomplete-capable."""
    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.tokens", ["fim", "fimbre", "fimbul"])]
                        )
    assert _one(tmp_path, "false-positive", write)["has_fim"] is False


# ------------------------------------------------------------- draft headers

HEAD = ["blk.4.nextn.head_h1.weight", "blk.4.nextn.head_h2.weight",
        "blk.4.nextn.head_h3.weight", "blk.4.nextn.output.weight"]


def test_a_file_carrying_its_own_draft_head_is_indexed_as_such(tmp_path):
    """`--spec-type draft-mtp` then needs one model, not a pair — and the head is
    already in the weights, so it costs no extra memory. Measured worth: 36-41
    tok/s to 95-99 tok/s at an unchanged footprint (§6.12.9)."""
    def write(p):
        _gguf_with_tensors(p, ["blk.0.attn_norm.weight", "output.weight"] + HEAD,
                           kv={"block_count": 4, "nextn_predict_layers": 1})
    # Not "mtp-inside": _AUX treats a *filename* starting `mtp-` as a sidecar
    # draft file, and the scanner would report the directory as holding nothing
    # loadable. That rule is right; the fixture name just has to not trip it.
    row = _one(tmp_path, "carries-head", write)
    assert row["nextn_layers"] == 1
    assert row["draft_tensors"] == len(HEAD)


def test_a_declared_head_the_quantiser_dropped_stays_visible_as_a_disagreement(tmp_path):
    """The two signals disagree, and the index must show both.

    The architecture asks for a draft layer; the tensor table does not carry one.
    Folding that into one boolean would report "no speculative decoding here"
    while the header still promises it — and the operator would reasonably ask
    where the head went.
    """
    def write(p):
        _gguf_with_tensors(p, ["blk.0.attn_norm.weight", "output.weight"],
                           kv={"block_count": 4, "nextn_predict_layers": 1})
    row = _one(tmp_path, "head-dropped", write)
    assert row["nextn_layers"] == 1
    assert row["draft_tensors"] == 0


def test_a_model_with_no_draft_anywhere_reads_as_zero_on_both(tmp_path):
    def write(p):
        _gguf_with_tensors(p, ["blk.0.attn_norm.weight", "output.weight"],
                           kv={"block_count": 4})
    row = _one(tmp_path, "plain", write)
    assert row["nextn_layers"] is None
    assert row["draft_tensors"] == 0


def test_the_two_draft_readers_agree_on_every_cached_model():
    """The index walk and the bench tool's walk are independent implementations
    of the same name rule; they are also the only check on each other."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "slot_bench", Path(__file__).resolve().parents[1] / "tools" / "slot_bench.py")
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)

    hub = mi.hub_dir()
    paths = [p for p in sorted(hub.glob("models--*/snapshots/*/*.gguf"))
             if "mmproj" not in p.name.lower()]
    if not paths:
        pytest.skip(f"no cached models under {hub} to cross-check")
    for path in paths:
        mine = modelmeta.draft_tensors(path)
        theirs = sb.nextn_tensors(path)
        if theirs is None:                       # a header the bench declines to read
            continue
        assert mine == theirs, f"{path.name}: index says {mine}, bench says {theirs}"


# ----------------------------------------------------------------- embedding

def test_pooling_type_reaches_the_index(tmp_path):
    """pooling_type is what separates a ranker from an embedder: llama.cpp answers
    /rerank with 501 for the second one. Measured on this box: qwen3-reranker
    (pooling_type 4) returned 2 results in 52 ms, embeddinggemma (1) returned 501.
    """
    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("testarch.pooling_type", 4)])
    assert _one(tmp_path, "ranker", write)["pooling_type"] == 4


def test_a_model_that_declares_no_pooling_leaves_the_column_null(tmp_path):
    def write(p):
        _write_fake_gguf(p, {"block_count": 4})
    assert _one(tmp_path, "no-pooling", write)["pooling_type"] is None


# -------------------------------------------------------- against the real box

def test_the_columns_read_true_on_models_that_actually_exist():
    """The fixtures prove the rules; the hub cache proves the rules apply.

    Skipped rather than failed if the operator has removed these repos, because
    the claim is about what a file states when it is present, not about what is
    installed today.
    """
    # scan_cache rather than rows(): this is about what the scanner reads out of a
    # file that exists, and it must not write to whatever database a test sandbox
    # happens to be pointing at.
    models, _problems = mi.scan_cache()
    if not models:
        pytest.skip(f"no cached models under {mi.hub_dir()} to check against")

    mtp = next((r for r in models if "MTP" in (r["id"] or "").upper()), None)
    if mtp is not None:
        assert mtp["draft_tensors"] > 0, f"{mtp['id']} carries no head after all"
        assert mtp["nextn_layers"] == 1
        assert mtp["has_fim"] is True, "a coder model with no FIM specials is news"

    embed = next((r for r in models if "embeddinggemma" in (r["id"] or "")), None)
    if embed is not None:
        assert embed["pooling_type"] == 1
        assert embed["has_fim"] is False, "an embedder with FIM tokens is news"


# --------------------------------------------------- the derived capability rule
#
# The columns above are facts the file states. capabilities() is the portal's
# reading of them, and it is the only place allowed to combine them — the models
# page, slot-picking and any future personality decision ask it the same question.
# What is pinned here is mostly the *language*: three states must stay three, and
# a disagreement between the header and the weights must not be smoothed over.

def _labels(row: dict) -> dict:
    return {c["label"]: c["tone"] for c in mi.capabilities(row)}


def test_autocomplete_capability_keeps_three_states():
    """True / False / None read as three different badges.

    Merging "no FIM specials" with "vocabulary unreadable" is the failure mode
    this whole column exists to avoid: the first is a reason not to start an
    autocomplete slot, the second is a reason to go look at the file.
    """
    assert _labels({"has_fim": True})["autocomplete"] == "ok"
    assert _labels({"has_fim": False})["no FIM"] == "mut"
    assert _labels({"has_fim": None})["fim?"] == "mut"
    assert _labels({})["fim?"] == "mut", "a missing column is unreadable, not absent"


def test_a_header_that_promises_a_head_the_weights_lack_is_a_warning():
    """The disagreement stays visible instead of collapsing to "no draft"."""
    both = _labels({"nextn_layers": 1, "draft_tensors": 4})
    assert any(l.startswith("draft x") for l in both), both
    dropped = _labels({"nextn_layers": 1, "draft_tensors": 0})
    assert dropped.get("head missing") == "warn", dropped
    assert not any(l.startswith("draft x") for l in dropped), \
        "a head that is not there must not be advertised as one"
    assert _labels({"nextn_layers": None, "draft_tensors": 0}) == \
        {k: v for k, v in _labels({}).items()}, "no draft anywhere: no draft badge"


def test_a_head_declared_only_by_tensor_names_still_counts():
    """llama.cpp finds the head by name; a header that omits the count does not
    hide it. Measured: the Qwopus files name blk.32.nextn.* without every export
    setting nextn_predict_layers."""
    caps = mi.capabilities({"nextn_layers": None, "draft_tensors": 4})
    assert [c["label"] for c in caps if c["tone"] == "ok"] == ["draft x4t"]


def test_ranker_and_embedder_are_labelled_apart():
    """The runtime refuses /rerank with 501 for the second, so a personality that
    wants a ranker has to be able to tell these two apart before it starts one."""
    assert "reranker" in _labels({"pooling_type": 4})
    assert "embedder" in _labels({"pooling_type": 1})
    assert "pool 7" in _labels({"pooling_type": 7}), \
        "an unknown enum value is reported, not guessed at"
    assert not any("pool" in l or "embed" in l or "rerank" in l
                   for l in _labels({"pooling_type": None}))


def test_capability_notes_explain_themselves():
    """Every badge is a hover note. A capability label with no explanation is how
    "no FIM" gets read as "cannot run /infill" — which is false, and costs a
    working slot."""
    row = {"has_fim": True, "nextn_layers": 1, "draft_tensors": 4,
           "pooling_type": 4, "vocab_size": 256000}
    for c in mi.capabilities(row):
        assert c["note"], c
        assert c["tone"] in ("ok", "warn", "info", "mut"), c
        assert len(c["note"]) > 30, f"{c['label']} is a label, not an explanation"


def test_the_column_as_stored_reads_the_same_as_the_column_as_computed():
    """SQLite stores a BOOLEAN column as 1/0, and `1 is True` is False.

    The first version of capabilities() compared with `is True`, so every row
    fetched from the table — as opposed to built by a scanner in memory — read as
    "vocabulary unreadable". The badge was wrong on the page and right in the
    unit test, which is the worst possible split.
    """
    assert mi.capabilities({"has_fim": 1})[0]["label"] == "autocomplete"
    assert mi.capabilities({"has_fim": 0})[0]["label"] == "no FIM"
    assert mi.capabilities({"has_fim": None})[0]["label"] == "fim?"


def test_a_real_sized_vocabulary_is_read_whole_by_the_reader(tmp_path):
    """The pin on MAX_STR_ARRAY itself, not on the badge it feeds.

    151 936 strings is the vocabulary size of a qwen3-class file: far past the
    64-entry limit the reader used to apply, and small enough to write in a
    fraction of a second. Asserting the count rather than "no placeholder" is
    deliberate — the placeholder was a plausible-looking string, which is why it
    passed every check that did not count.
    """
    n = 151_936
    toks = [f"t{i}" for i in range(n)]
    toks[151_000] = _special("fim_middle")

    def write(p):
        _write_raw_gguf(p, [("general.architecture", "testarch"),
                            ("testarch.block_count", 4),
                            ("tokenizer.ggml.tokens", toks)])
    row = _one(tmp_path, "real-vocab", write)
    assert row["vocab_size"] == n
    assert row["has_fim"] is True
    assert modelmeta.sniff_gguf(tmp_path / "real-vocab" / "real-vocab.gguf")[
        "vocab_size"] == n, "the reader and the index row disagree about the same file"
