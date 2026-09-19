"""modelmeta: GGUF/safetensors header sniffing + ComfyUI category rules.

Fixtures are synthetic files built in-test (a few hundred bytes), so the
tests are fast, offline, and deterministic.
"""
import json
import os
import shutil
import struct
import tempfile

# Isolation (same contract as the other test files): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-mm-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402

from app.core import modelmeta  # noqa: E402


# ------------------------------------------------------------------ fixtures

def make_gguf(path, name="Test Model 1B", arch="llama", ctx=4096,
              tensors=123):
    """A minimal but valid (big-endian) GGUF: magic, version 3, three KV
    pairs — general.name, general.architecture, <arch>.context_length."""
    kvs = []

    def add_str(key, val):
        kb, vb = key.encode(), val.encode()
        kvs.append(struct.pack(">Q", len(kb)) + kb +
                   struct.pack(">I", modelmeta.T_STRING) +
                   struct.pack(">Q", len(vb)) + vb)

    def add_u32(key, val):
        kb = key.encode()
        kvs.append(struct.pack(">Q", len(kb)) + kb +
                   struct.pack(">I", 4) + struct.pack(">I", val))

    add_str("general.name", name)
    add_str("general.architecture", arch)
    add_u32(f"{arch}.context_length", ctx)

    # b"GGUF" as LE u32 is 0x46554747; version bytes 00 00 00 03 make the
    # reader pick the big-endian layout (raw_version & 0xFFFF == 0).
    header = (b"GGUF" + b"\x00\x00\x00\x03" +
              struct.pack(">QQ", tensors, 3))
    with open(path, "wb") as f:
        f.write(header + b"".join(kvs) + b"\x00" * 64)


def make_safetensors(path, keys, metadata=None):
    """Minimal valid safetensors: 8-byte LE header length + JSON + padding +
    dummy payload bytes (sniffing never reads the payload)."""
    hdr = {k: {"dtype": "F32", "shape": [1, 4]} for k in keys}
    if metadata:
        hdr["__metadata__"] = metadata
    hb = json.dumps(hdr).encode()
    pad = (8 - len(hb) % 8) % 8
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(hb) + pad) + hb + b" " * pad +
                b"\x00" * 32)


@pytest.fixture
def tmp():
    d = tempfile.mkdtemp(prefix="aipp-mm-fx-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# -------------------------------------------------------------------- GGUF

def test_sniff_gguf_fields(tmp):
    p = f"{tmp}/tiny.gguf"
    make_gguf(p, name="Qwen3 Test", arch="qwen3", ctx=32768, tensors=42)
    meta = modelmeta.sniff_gguf(p)
    assert meta is not None
    assert meta["format"] == "gguf"
    assert meta["name"] == "Qwen3 Test"
    assert meta["arch"] == "qwen3"
    assert meta["context_length"] == 32768
    assert meta["tensor_count"] == 42


def test_sniff_gguf_rejects_non_gguf(tmp):
    p = f"{tmp}/junk.gguf"
    with open(p, "wb") as f:
        f.write(os.urandom(1024))
    assert modelmeta.sniff_gguf(p) is None


def test_sniff_gguf_rejects_truncated(tmp):
    p = f"{tmp}/trunc.gguf"
    make_gguf(p)
    data = open(p, "rb").read()
    with open(p, "wb") as f:
        f.write(data[:40])  # magic + version + counts, but most of KV gone
    assert modelmeta.sniff_gguf(p) is None


def test_sniff_gguf_rejects_bad_version(tmp):
    p = f"{tmp}/v9.gguf"
    make_gguf(p)
    data = bytearray(open(p, "rb").read())
    data[7] = 9  # version 9
    with open(p, "wb") as f:
        f.write(bytes(data))
    assert modelmeta.sniff_gguf(p) is None


# ------------------------------------------------------------ safetensors

def test_sniff_safetensors_keys_and_metadata(tmp):
    p = f"{tmp}/w.safetensors"
    keys = ["cond_stage_model.transformer.text_model.embed_tokens.weight"]
    make_safetensors(p, keys, metadata={"format": "pt"})
    meta = modelmeta.sniff_safetensors(p)
    assert meta is not None
    assert meta["format"] == "safetensors"
    assert meta["num_tensors"] == 1
    assert meta["keys"] == keys
    assert meta["metadata"] == {"format": "pt"}


def test_sniff_safetensors_rejects_gguf(tmp):
    p = f"{tmp}/g.gguf"
    make_gguf(p)
    assert modelmeta.sniff_safetensors(p) is None


def test_sniff_file_dispatch(tmp):
    pg, ps, pj = f"{tmp}/a.gguf", f"{tmp}/b.safetensors", f"{tmp}/c.bin"
    make_gguf(pg, name="A")
    make_safetensors(ps, ["model.diffusion_model.blocks.0.weight"])
    with open(pj, "wb") as f:
        f.write(os.urandom(512))
    assert modelmeta.sniff_file(pg)["format"] == "gguf"
    assert modelmeta.sniff_file(ps)["format"] == "safetensors"
    assert modelmeta.sniff_file(pj) is None


# ---------------------------------------------------------------- category

def test_classify_loras():
    assert modelmeta.classify_safetensors(
        ["lora_down.weight", "lora_up.weight"]) == "loras"
    assert modelmeta.classify_safetensors(
        ["diffusion_model.blocks.0.lora_down.weight"]) == "loras"


def test_classify_vae():
    keys = ["encoder.mid.block1.conv1.weight", "decoder.mid.block2.conv2.weight"]
    assert modelmeta.classify_safetensors(keys) == "vae"


def test_classify_clip():
    keys = ["cond_stage_model.transformer.text_model.encoder.layers.0.weight"]
    assert modelmeta.classify_safetensors(keys) == "clip"


def test_classify_diffusion_models():
    keys = ["diffusion_model.transformer_blocks.0.attn.q.weight"]
    assert modelmeta.classify_safetensors(keys) == "diffusion_models"


def test_classify_checkpoints():
    keys = ["model.diffusion_model.input_blocks.0.0.weight"]
    assert modelmeta.classify_safetensors(keys) == "checkpoints"


def test_classify_upscale():
    keys = [f"model.{i}.weight" for i in range(8)]
    assert modelmeta.classify_safetensors(keys, size_bytes=10 * 1024 * 1024
                                          ) == "upscale_models"
    # same keys but a big file is a checkpoint, not an upscaler
    assert modelmeta.classify_safetensors(keys, size_bytes=5 * 1024**3
                                          ) == "checkpoints"


def test_classify_unknown_returns_none():
    assert modelmeta.classify_safetensors([]) is None
    assert modelmeta.classify_safetensors(["mystery.tensor.weight"]
                                          ) is None


def test_weight_exts_and_non_model_categories():
    assert ".safetensors" in modelmeta.WEIGHT_EXTS
    assert ".ckpt" in modelmeta.WEIGHT_EXTS
    assert ".gguf" not in modelmeta.WEIGHT_EXTS
    assert modelmeta.NON_MODEL_CATEGORIES == {"configs"}


# ------------------------------------------------- tokenizer-derived role facts
def test_no_marker_pattern_matches_the_empty_string():
    """The bug this pins. An alternation of delimiters written as
    `<\\|tool_call|>` has an unescaped trailing pipe, so the regex engine read it as
    "`<|tool_call` OR empty string" and matched at offset 0 of every template. Every
    model in the library reported tool support. A pattern whose alternation can be
    empty does not fail — it reports true for all inputs, forever.
    """
    for pattern, label in modelmeta._TEMPLATE_MARKERS:
        assert pattern.search("") is None, f"{label} pattern matches the empty string"
        # and it must not fire on a template that mentions nothing of the sort
        innocuous = '{{ "<|begin_of_text|>" }}{{ message }}'
        assert pattern.search(innocuous) is None, \
            f"{label} pattern matches a template that does not speak it: " \
            f"{pattern.search(innocuous).group(0)!r}"


CHATML_OPEN = "<" + "|im_start|" + ">"  # ChatML turn opener, spelled to survive tooling

def test_template_markers_read_real_protocols():
    # A template declares a role by spelling the literal out; `{{ message.role }}`
    # interpolates whatever is passed in and declares nothing, so the bare loop is
    # correctly [] — that is the same rule that rejects a `tools` loop variable.
    silent_loop = ("{% for message in messages %}{{ message.role }}\n"
                   "{{ message.content }}{% endfor %}")
    assert modelmeta._template_markers(silent_loop) == []

    chatml = ("{% for message in messages %}\n"
              "{{ '" + CHATML_OPEN + "user\\n{{ message.content }}' }}")
    assert "chat" in modelmeta._template_markers(chatml)
    assert "tools" not in modelmeta._template_markers(chatml)

    tool_tpl = chatml + '\n{% if tools %}{{ "<tool_call>" }}{% endif %}'
    mk = modelmeta._template_markers(tool_tpl)
    assert "tools" in mk

    # a tool RESULT role is distinct from the model emitting a call
    results_only = '{% if message["role"] == "tool" %}{{ message.content }}{% endif %}'
    mk2 = modelmeta._template_markers(results_only)
    assert "tool-results" in mk2 and "tools" not in mk2


def test_a_loop_variable_named_tools_is_not_tool_support():
    """13 of 15 models on this box claimed tool support because `"tools" in template`
    matched the Jinja loop variable. A template mentions a thing because it renders
    it, which is not evidence the weights were trained to produce it."""
    loop_only = '{% for tool in tools %}{{ tool.function.name }}{% endfor %}'
    assert modelmeta._template_markers(loop_only) == []


def test_missing_template_is_reported_as_absent_not_guessed():
    """A base model has no chat template. That is the notable case — it will not hold
    a chat turn however it is prompted — and it must read as absence, not as the
    default for a chat model."""
    assert modelmeta._template_markers("") == []
    assert modelmeta._template_markers(None) == []
    assert modelmeta._template_markers("   ") == []
