"""The bench tool's speculative-decoding decisions.

Two of these rules exist because a run hung. A draft file is not a model: benching
`Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF:Q8_0` standalone started a server that never
answered, and the only record of it was a script that had to be killed. So the tool
decides from the bytes — does the target carry its own `*.nextn.*` head, or is there
a draft file beside it — and refuses with a line in the report otherwise.

Pure helpers only: nothing here starts a server or touches the operator's ~/ai.
"""

import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_tool():
    spec = importlib.util.spec_from_file_location("slot_bench", ROOT / "tools" / "slot_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gguf(path: Path, tensor_names) -> Path:
    """A GGUF with real tensor-info records — a head lives in the tensor names."""

    def s(text: str) -> bytes:
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw

    body = s("general.architecture") + struct.pack("<I", 8) + s("qwen2")
    n_tensors = len(tensor_names)
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", n_tensors) + struct.pack("<Q", 1)
    out += body
    for i, name in enumerate(tensor_names):
        out += s(name) + struct.pack("<I", 2) + struct.pack("<Q", 4) + struct.pack("<Q", 4)
        out += struct.pack("<I", 8) + struct.pack("<Q", i * 1024)
    path.write_bytes(out)
    return path


# ---------------------------------------------------------------- the MTP head

def test_a_file_with_its_own_nextn_head_needs_no_separate_draft(tmp_path):
    tool = load_tool()
    model = _gguf(tmp_path / "Qwopus3.5-4B-Coder-MTP-Q8_0.gguf",
                  ["token_embd.weight", "blk.32.nextn.head_h.weight",
                   "blk.32.nextn.output.weight", "blk.31.attn_q.weight"])
    assert tool.nextn_tensors(model) == 2


def test_a_plain_quant_reports_no_head_rather_than_a_guess(tmp_path):
    tool = load_tool()
    model = _gguf(tmp_path / "Qwopus3.5-4B-coder-Q8_0.gguf",
                  ["token_embd.weight", "blk.31.attn_q.weight"])
    assert tool.nextn_tensors(model) == 0


def test_an_unparseable_header_is_unknown_not_zero(tmp_path):
    """Zero means 'no head, go look for a draft'; garbage means 'we could not read it'."""
    tool = load_tool()
    junk = tmp_path / "junk.gguf"
    junk.write_bytes(b"GGUF\x03\x00\x00\x00\xff\xff\xff\xff\xff\xff\xff\xff")
    assert tool.nextn_tensors(junk) is None


# ---------------------------------------------------------------- the draft file

def test_a_draft_beside_the_target_is_found_and_the_target_is_not(tmp_path):
    tool = load_tool()
    base = _gguf(tmp_path / "model-Q8_0.gguf", ["a.weight"])
    draft = _gguf(tmp_path / "mtp-model-Q8_0.gguf", ["blk.32.nextn.output.weight"])
    assert tool.find_draft(base) == draft
    # A file whose own name says "draft" must not be handed back as its own -md;
    # llama-server then loads the same file twice and the run looks like a hang.
    assert tool.find_draft(draft) is None


def test_a_projector_is_never_mistaken_for_a_draft(tmp_path):
    tool = load_tool()
    base = _gguf(tmp_path / "model-Q8_0.gguf", ["a.weight"])
    _gguf(tmp_path / "model-mtp-mmproj-Q8_0.gguf", ["v.weight"])
    assert tool.find_draft(base) is None


def test_draft_markers_are_case_sensitive_the_way_the_runtime_reads_them():
    """This used to be a case-insensitive test, and the case-insensitive rule was
    wrong: `llama-server --cache-list` (pinned in test_modelindex) lists
    `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` as a loadable model, and the portal agrees.
    Lower-casing here refused to bench that model and accepted an imatrix file."""
    tool = load_tool()
    assert not tool._draft_like("Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF:Q8_0")
    assert not tool._draft_like("Qwopus3.5-4B-Coder-MTP-Q8_0.gguf")
    assert tool._draft_like("model-mtp-Q4_K_M.gguf")
    assert tool._draft_like("eagle3-model.gguf")
    assert not tool._draft_like("Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0")
    assert not tool._draft_like("model-Q4_K_M.gguf")
    # one rule, shared with the portal — not a second list that can drift
    from app.core import modelindex
    assert tool._DRAFT_MARKERS == tuple(
        m for m in modelindex.NOT_A_MODEL_MARKERS if m != "mmproj")
