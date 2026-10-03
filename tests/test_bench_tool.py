"""tests for the benchmark harness — only the parts that made measurement calls.

`tools/slot_bench.py` is the instrument every slot decision in the plan was read
off, so a bug in it silently becomes a wrong decision about the library. Each
rule pinned here was learned from a run that failed:

  * pick_repo_file  — reported a model already on disk as "not cached" because
                      the quant tag was a suffix, not a name prefix
  * AUX / _draft_like — kept its own marker list matched case-insensitively, so
                      it refused to bench a model the router loads
  * find_draft      — handed the server its own MTP file as its draft
  * nextn_tensors   — `--spec-type draft-mtp` means "use the head inside this
                      file" or "find a second file", and only the file says which
  * embedding_vector— /embedding answered in a shape the reader assumed was the
                      OpenAI one, so a working 768-dim embedder reported dim=1
  * _scan_vocab     — a card claiming FIM while the vocab has no FIM tokens makes
                      every /infill sample meaningless

The tool is loaded by path, not imported as a package member: importing it must
stay a no-op (no scan, no network, no subprocess).
"""

import argparse
import ast
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

# gguf_fixture, never another test module: test_modelindex claims a password and a
# throwaway home with setdefault at import, and importing it from here (which
# collects earlier) wins that race for the whole session — 56 failures and 12
# errors of 401s in files that never touched auth. See tests/gguf_fixture.py.
import gguf_fixture as fx             # noqa: E402


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "slot_bench", ROOT / "tools" / "slot_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sb = _load_tool()


def _markers() -> tuple:
    """modelindex's marker list, imported inside the test: this module must not
    import app/* at import time either, since the modules that own the throwaway
    home and password claim them at theirs."""
    from app.core import modelindex
    return modelindex.NOT_A_MODEL_MARKERS


# --------------------------------------------------------- one naming rule only
def test_the_tool_and_the_portal_share_one_file_rule():
    """The runtime's rule is pinned against `llama-server --cache-list` in
    test_modelindex. A second copy here drifted (case-insensitive, its own list)
    and the bench refused `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf`, which the router
    loads, while accepting an imatrix calibration file as a model."""
    for marker in _markers():
        assert sb.AUX.search(f"anything-{marker}rest.gguf"), marker
    assert not sb.AUX.search("Qwopus3.5-4B-Coder-MTP-Q8_0.gguf")   # a model
    assert not sb._draft_like("Qwopus3.5-4B-Coder-MTP-Q8_0.gguf")
    assert sb._draft_like("mtp-Qwythos-27B-Q6_K.gguf")             # a sidecar
    assert not sb._draft_like("codegemma-2b-mmproj-Q5_K_M.gguf")   # not a draft


# ------------------------------------------------------------- hub file picking
def test_the_quant_tag_position_decides_not_the_name_prefix():
    """All four of these name one model to llama.cpp. The old test was
    `path.upper().startswith(quant[:2])`, which matched only the tag directory,
    so ggml-org's embedder "downloaded" nothing and the run reported a file that
    was already on disk as missing."""
    cases = [
        ([{"path": "embeddinggemma-300M-Q8_0.gguf"}], "Q8_0",
         "embeddinggemma-300M-Q8_0.gguf"),
        ([{"path": "qwen3-reranker-0.6b-q8_0.gguf"}], "Q8_0",
         "qwen3-reranker-0.6b-q8_0.gguf"),                      # "_" not "-"
        ([{"path": "IQ3_S/repo-IQ3_S-00001-of-00002.gguf"},
          {"path": "IQ3_S/repo-IQ3_S-00002-of-00002.gguf"},
          {"path": "Q5_K_M/repo-Q5_K_M.gguf"}], "IQ3_S",
         "IQ3_S/repo-IQ3_S-00001-of-00002.gguf"),               # tag directory
        ([{"path": "codegemma-2b-Q5_K_M.gguf"},
          {"path": "codegemma-2b-Q4_K_M.gguf"}], "Q4_K_M",
         "codegemma-2b-Q4_K_M.gguf"),
    ]
    for files, quant, want in cases:
        got = sb.pick_repo_file(files, quant)
        assert got == want, f"{quant}: got {got}, want {want}"


def test_a_projector_or_a_calibration_file_is_never_the_model():
    assert sb.pick_repo_file([{"path": "codegemma-2b-Q5_K_M.gguf"},
                              {"path": "codegemma-2b-mmproj-Q5_K_M.gguf"}],
                             "Q5_K_M") == "codegemma-2b-Q5_K_M.gguf"
    assert sb.pick_repo_file([{"path": "mmproj-F16.gguf"},
                              {"path": "eagle3-model.gguf"},
                              {"path": "model-imatrix-Q8_0.gguf"}], "Q8_0") is None
    assert sb.pick_repo_file([{"path": "README.md"}], "Q8_0") is None


# ---------------------------------------------------------------------- drafts
def test_a_model_is_never_its_own_draft(tmp_path):
    """The Qwopus MTP file contains "MTP" in its own name; an unguarded glob
    handed llama-server the target as --spec-draft-model and the run hung."""
    target = tmp_path / "Qwopus3.5-4B-Coder-MTP-Q8_0.gguf"
    target.write_bytes(b"GGUF")
    assert sb.find_draft(target) is None, "a model is not its own draft"

    other = tmp_path / "Qwopus3.5-4B-Coder-Q8_0.gguf"
    other.write_bytes(b"GGUF")
    assert sb.find_draft(other) is None


def test_a_sidecar_draft_beside_the_target_is_found(tmp_path):
    d = tmp_path / "unsloth"
    d.mkdir()
    sibling = d / "mtp-Qwythos-27B-Q6_K.gguf"
    sibling.write_bytes(b"GGUF")
    plain = d / "Qwythos-27B-Q6_K.gguf"
    plain.write_bytes(b"GGUF")
    (d / "Qwythos-27B-mmproj-Q6_K.gguf").write_bytes(b"GGUF")
    assert sb.find_draft(plain) == sibling      # projector is not a draft


def test_the_mtp_head_can_live_inside_the_target(tmp_path):
    """Two repos, two wirings, and only the file says which: Jackrong's MTP file
    is the base model plus blk.N.nextn.* (one --model is enough), unsloth's puts
    the head in a separate mtp-*.gguf (needs --spec-draft-model)."""
    own = tmp_path / "Qwopus3.5-4B-Coder-MTP-Q8_0.gguf"
    fx._gguf_with_tensors(own, ["blk.31.ffn_down.weight", "blk.32.nextn.output.weight",
                             "blk.32.nextn.head_h.0.weight"])
    assert sb.nextn_tensors(own) == 2, sb.nextn_tensors(own)

    plain = tmp_path / "Qwopus3.5-4B-Coder-Q8_0.gguf"
    fx._gguf_with_tensors(plain, ["blk.31.ffn_down.weight", "output.weight"])
    assert sb.nextn_tensors(plain) == 0

    # a truncated or non-GGUF file is "unknown", not "no head"
    junk = tmp_path / "junk.gguf"
    junk.write_bytes(b"not a gguf at all")
    assert sb.nextn_tensors(junk) is None


# ---------------------------------------------------------- embedding responses
def test_embedding_vector_handles_every_shape_the_route_uses():
    """Measured on b11146: POST /embedding answers `[{"index":0,"embedding":
    [[...]]}]`. Reading it as the OpenAI dict raised AttributeError, and taking
    the length of the wrong level reported dim=1 — a working embedder looked
    broken."""
    vec = [0.1, 0.2, 0.3]
    for body in (
        [{"index": 0, "embedding": [vec]}],          # measured on b11146
        {"embedding": [vec]},                        # non-OAI, nested per input
        {"embedding": vec},                          # non-OAI, single input
        [{"index": 0, "embedding": vec}],            # OpenAI item, no wrapper
        {"data": [{"embedding": vec}]},              # OpenAI
    ):
        assert sb.embedding_vector(body) == vec, body
    assert sb.embedding_vector({"error": "no pooling"}) == []


# ------------------------------------------------------------------- /infill
def test_the_infill_body_is_the_only_shape_the_route_accepts():
    """/infill reads input_prefix / input_suffix / input_middle. Sending prompt /
    suffix is answered 500 "key 'input_suffix' not found" (server-context.cpp),
    and the 4-at-once probe did exactly that: every report said "fewer than 2 of 4
    concurrent requests succeeded" while the per-language rows — which used the
    right keys — looked fine. The burst number the autocomplete slot needs was
    never measured, and the swallowed exception hid why."""
    body = sb.fim_payload("def f():\n", "\n    return 1", 64)
    assert body["input_prefix"] == "def f():\n"
    assert body["input_suffix"] == "\n    return 1"
    assert "prompt" not in body and "suffix" not in body


def test_every_infill_call_sends_that_body():
    """One builder is only a rule if the call sites are checked."""
    import inspect
    src = inspect.getsource(sb.bench_fim)
    assert src.count('srv.post("/infill"') == src.count("fim_payload("), src.count('srv.post("/infill"')
    assert '"prompt"' not in src and '"suffix"' not in src


# ----------------------------------------------------------------- FIM tokens
def _append_top_level(path: pathlib.Path, key: str, value) -> None:
    """Append one unprefixed KV entry of a chosen type and bump kv_count.
    _append_kv_key fixes the type to bool; the vocab keys are string arrays."""
    import struct
    raw = bytearray(path.read_bytes())
    n = struct.unpack_from("<Q", raw, 16)[0]
    struct.pack_into("<Q", raw, 16, n + 1)
    path.write_bytes(bytes(raw) + fx._enc_str(key) + fx._enc_value(value))


def test_fim_tokens_come_from_the_vocab_not_the_card(tmp_path):
    """The model card says FIM=yes; the request then ignores the markers because
    the vocab has none. Only the header answers the question."""
    withfim = tmp_path / "withfim.gguf"
    fx._write_fake_gguf(withfim, {"block_count": 2})
    _append_top_level(withfim, "tokenizer.ggml.tokens", ["hello", "world", ""])
    _append_top_level(withfim, "tokenizer.ggml.added_tokens", ["", ""])
    toks, added = sb._scan_vocab(withfim)
    assert toks == ["hello", "world", ""], toks
    assert added == ["", ""], added

    plain = tmp_path / "plain.gguf"
    fx._write_fake_gguf(plain, {"block_count": 2})
    _append_top_level(plain, "tokenizer.ggml.tokens", ["hello", "world"])
    toks, added = sb._scan_vocab(plain)
    assert toks == ["hello", "world"] and added == [], (toks, added)


# ------------------------------------------------- residency flags (-lm / -lzm / -ngl)
#
# The operator runs the router with `-lm mmap -ngl 99 --lazy-mode on` because that is what the
# ISTA release notes advise, while every number in the plan was measured without them. So the
# bench must be able to pass them, and it must pass values the installed build actually accepts.


def _args(sb, **over):
    ns = argparse.Namespace(load_mode="auto", lazy_mode="auto", gpu_layers=99)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_an_untouched_run_still_carries_no_residency_flags():
    """Reports from before this flag existed stay comparable, byte for byte."""
    assert sb.runtime_flags(_args(sb)) == []


def test_the_operators_flags_reach_the_worker_verbatim():
    flags = sb.runtime_flags(_args(sb, load_mode="mmap", lazy_mode="on"))
    assert flags == ["--load-mode", "mmap", "--lazy-mode", "on"]


def test_the_value_lists_are_the_installed_builds_and_not_invented():
    """`--lazy-mode none` does not exist: the build takes on|auto|off. A wrong value is not
    ignored — llama-server exits at startup and the run reports a dead model."""
    import glob
    builds = sorted(glob.glob("/home/cortana/ai/releases/llama.cpp-b*"))
    if not builds:
        import pytest
        pytest.skip("no llama.cpp release on this box")
    import subprocess
    help_text = subprocess.run([builds[-1] + "/llama-server", "--help"],
                               capture_output=True, text=True, timeout=120).stdout
    for flag, choices in (("--load-mode", sb.LOAD_MODES), ("--lazy-mode", sb.LAZY_MODES)):
        allowed = _help_values(help_text, flag)
        assert allowed, f"{flag} is not in the installed build's help"
        assert set(choices) <= allowed, f"{flag}: {set(choices) - allowed} are not accepted values"


def _help_values(help_text: str, flag: str) -> set[str]:
    """The value list llama.cpp prints under one option.

    Scanned line by line rather than by regex block: usage strings wrap, and a wrapped tail
    ("...may reduce\n pageouts if not using mlock") is not a value line but is not the next option
    either. A regex repetition stops at the wrap and reports a legal value as invented.
    """
    lines = help_text.splitlines()
    at = next((i for i, l in enumerate(lines)
               if re.search(r"(^|\s)" + re.escape(flag) + r"(\s|MODE|N|$)", l)), None)
    if at is None:
        return set()
    out: set[str] = set()
    for line in lines[at + 1:]:
        if re.match(r"^\s*-{1,2}[A-Za-z]", line) and not re.match(r"^\s+-", line):
            break  # next option
        m = re.match(r"^\s+- ([A-Za-z0-9+_]+)[:,]", line)
        if m:
            out.add(m.group(1))
    return out


# ------------------------------------------- naming a cached model (the :MTP trap)
#
# `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:IQ3_S-mtp` was asked for, and the tool
# said "not cached" while printing a hub path -- for a file sitting in the cache. The
# tag llama.cpp uses is the last dash-separated token of the file name (`llama-server
# --cache-list` prints `:MTP`), and a message that only lists hub files sends the
# operator to download a model they already own.

def test_a_missed_quant_names_the_ids_that_do_exist(tmp_path, monkeypatch):
    f = tmp_path / "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"
    f.write_bytes(b"GGUF")
    monkeypatch.setattr(sb, "cached_paths", lambda repo: {"MTP": f})
    monkeypatch.setattr(sb, "hub_files", lambda repo: [{"path": "Swift-1.5-IQ3_S-mtp.gguf"}])
    text = "\n".join(sb.miss_hint("ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF", "IQ3_S-mtp"))
    assert "ids llama.cpp offers for this repo: ['MTP']" in text
    assert "try :MTP" in text, "the hint must name the id that works, not just the problem"
    reask = "\n".join(sb.miss_hint("ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF", "IQ3_S_MTP"))
    assert "try :MTP" in reask, "- and _ spellings of one tag mean one file"
    assert sb.cached_paths("x")["MTP"] == f


def test_a_genuinely_absent_model_does_not_invent_an_id(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "cached_paths", lambda repo: {})
    monkeypatch.setattr(sb, "hub_files", lambda repo: [{"path": "Q8_K_XL/x.gguf"}])
    text = "\n".join(sb.miss_hint("org/Repo", "Q8_K_XL"))
    assert "ids llama.cpp offers for this repo: none" in text
    assert "try :" not in text


def test_an_ambiguous_id_is_not_recommended_as_a_fix(tmp_path, monkeypatch):
    """`repo:TAG` is resolved by the regex TAG[.-] over the repo's file list, first
    match wins — measured on this box, where the runtime's own id `:MTP` for a cached
    IQ3_S file started a 12 GiB download of IQ2_S-mtp.gguf. A hint that says "try :MTP"
    for that repo sends the operator after the wrong weights."""
    f = tmp_path / "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"
    f.write_bytes(b"GGUF")
    monkeypatch.setattr(sb, "cached_paths", lambda repo: {"MTP": f})
    monkeypatch.setattr(sb, "hub_files", lambda repo: [
        {"path": "IQ2_S-mtp/Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ2_S-mtp.gguf"},
        {"path": "IQ3_S-mtp/Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"},
    ])
    text = "\n".join(sb.miss_hint("ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF", "IQ3_S-mtp"))
    assert "try :MTP" not in text
    assert "2 hub files match" in text
    assert str(f) in text, "the safe answer is the path, not the id"
