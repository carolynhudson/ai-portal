"""Fast model-file metadata sniffing — header-only, no full parse.

Two formats matter on this box:

- **GGUF** (llama.cpp): 8-byte magic, version, tensor/kv counts, then the
  metadata key/value section. We walk only the KV section (mmap'd) — the same
  approach as the gguf-indexer project, which deliberately skips tensor
  descriptors because a 27B model has hundreds of them.
- **Safetensors** (ComfyUI): 8-byte little-endian header length followed by a
  JSON header whose *keys* are the tensor names — enough to classify which
  ComfyUI model subfolder the file belongs to, without reading any weights.

Both cost a few MB of reads at most, so multi-GB files are cheap to inspect.
"""
from __future__ import annotations

import json
import mmap
import re
import struct
from pathlib import Path

GGUF_MAGIC = 0x46554747  # b"GGUF"
_SCALAR_FMT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f",
               7: "?", 10: "Q", 11: "q", 12: "d"}
_SCALAR_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                10: 8, 11: 8, 12: 8}
T_STRING, T_ARRAY = 8, 9

# ComfyUI model subfolders that hold weight files (configs/ and flag files
# like put_*_here are deliberately excluded from the inventory).
WEIGHT_EXTS = (".safetensors", ".ckpt", ".bin")
NON_MODEL_CATEGORIES = {"configs"}


# ------------------------------------------------------------------ GGUF
def _gguf_read_value(mm, bo: str, offs: int, ftype: int):
    def read_bytes(n: int) -> bytes:
        nonlocal offs
        b = bytes(mm[offs:offs + n])
        if len(b) != n:
            raise EOFError("unexpected end of GGUF metadata")
        offs += n
        return b

    if ftype == T_STRING:
        n, = struct.unpack_from(bo + "Q", mm, offs); offs += 8
        return read_bytes(n).decode("utf-8", "replace"), offs
    if ftype == T_ARRAY:
        etype, = struct.unpack_from(bo + "I", mm, offs); offs += 4
        n, = struct.unpack_from(bo + "Q", mm, offs); offs += 8
        if etype == T_STRING:
            if n > 64:  # e.g. giant tag lists — skip the bodies
                fmt = bo + "Q"
                for _ in range(n):
                    ln, = struct.unpack_from(fmt, mm, offs)
                    offs += 8 + ln
                return f"<{n} strings skipped>", offs
            out = []
            for _ in range(n):
                ln, = struct.unpack_from(bo + "Q", mm, offs); offs += 8
                out.append(read_bytes(ln).decode("utf-8", "replace"))
            return out, offs
        if etype not in _SCALAR_FMT:
            raise ValueError(f"unsupported array element type {etype}")
        size = _SCALAR_SIZE[etype] * n
        fmt = bo + str(n) + _SCALAR_FMT[etype]
        return list(struct.unpack(fmt, read_bytes(size))), offs
    if ftype in _SCALAR_FMT:
        v, = struct.unpack_from(bo + _SCALAR_FMT[ftype], mm, offs)
        offs += _SCALAR_SIZE[ftype]
        return v, offs
    raise ValueError(f"unsupported field type {ftype}")


def _gguf_parse_kv(mm, bo: str, kv_count: int, aligned: bool) -> dict:
    offs = 24
    fields: dict = {}
    for _ in range(kv_count):
        pad = (-offs) % 8 if aligned else 0
        n, = struct.unpack_from(bo + "Q", mm, offs + pad); offs += pad + 8
        name = bytes(mm[offs:offs + n])
        if not name.isalpha() and not name:
            raise ValueError("corrupt key name — alignment mismatch?")
        name = name.decode("utf-8", "replace")
        offs += n
        ftype, = struct.unpack_from(bo + "I", mm, offs); offs += 4
        fields[name], offs = _gguf_read_value(mm, bo, offs, ftype)
        if aligned:
            offs += (-offs) % 8
    return fields


def _header_buffer(f):
    """mmap the file when the filesystem allows, else fall back to a plain
    read of the first few MiB (some filesystems — tmpfs under sandboxing,
    certain FUSE mounts — refuse mmap). Both expose the bytes-like interface
    (slicing, struct.unpack_from) the KV walker needs. Only the metadata
    head is ever touched, so the cap is generous."""
    try:
        return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return f.read(4 * 1024 * 1024)


def sniff_gguf(path) -> dict | None:
    """Return {arch, name, size_label, context_length, tensor_count, …} or
    None if the file is not GGUF. Only the metadata head is touched."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            mm = _header_buffer(f)
            try:
                magic, = struct.unpack_from("<I", mm, 0)
                if magic != GGUF_MAGIC:
                    return None
                raw_version, = struct.unpack_from("<I", mm, 4)
                bo = ">" if (raw_version & 0xFFFF) == 0 else "<"
                version, = struct.unpack_from(bo + "I", mm, 4)
                if version not in (2, 3):
                    return None
                tensor_count, kv_count = struct.unpack_from(bo + "QQ", mm, 8)
                # llama.cpp writes unpadded KV sections; other producers may
                # 8-byte-align (v3 spec) — try plain first, then aligned.
                try:
                    fields = _gguf_parse_kv(mm, bo, kv_count, aligned=False)
                except (ValueError, EOFError, struct.error):
                    fields = _gguf_parse_kv(mm, bo, kv_count, aligned=True)
                ap = fields.get("general.architecture", "") + "."
                out = {
                    "format": "gguf",
                    "arch": fields.get("general.architecture"),
                    # What the file declares itself to BE, and what encoder it carries.
                    # Read from the file rather than inferred from the filename: every
                    # mmproj on this box declares `general.type = mmproj` and
                    # `clip.has_vision_encoder = True`, so "this projector is a vision
                    # projector" is a fact the file states, not a guess from `mmproj-`.
                    # Audio projectors would declare a different encoder; none of the 8
                    # here do, and nothing here invents one that is not declared.
                    "declared_type": fields.get("general.type") or None,
                    "vision_encoder": fields.get("clip.has_vision_encoder"),
                    "name": fields.get("general.name") or None,
                    "size_label": fields.get("general.size_label") or None,
                    "context_length": _as_int(fields.get(ap + "context_length")),
                    "tensor_count": tensor_count,
                    "organization": fields.get("general.organization") or None,
                    "quantized_by": fields.get("general.quantized_by") or None,
                    # Architecture and quant are not enough to predict what a
                    # model costs — the KV cache is a function of these, and for
                    # hybrid models getting one number wrong overstates it 4×
                    # (PORTAL_PLAN §4.9). Picked from the header rather than
                    # guessed from the arch name, because arch names lie.
                    "block_count": _as_int(_first(fields, ap, "block_count")),
                    "embedding_length": _as_int(_first(fields, ap, "embedding_length")),
                    "rope_dimension_count": _as_int(_first(fields, ap, "rope_dimension_count")),
                    "full_attention_interval": _as_int(
                        _first(fields, ap, "full_attention_interval", "attention.interval")),
                    # Head counts are sometimes a per-layer ARRAY rather than a
                    # scalar: gemma4 writes head_count_kv as 64 entries (16 or 4,
                    # sliding-window vs full-attention layers) and laguna writes
                    # head_count as 48. That is real hardware diversity, not missing
                    # data, and reading it as None silently drops the model from the
                    # memory estimate. kv_heads_total is the sum across layers —
                    # exactly the quantity the KV cache is proportional to — so the
                    # estimator adds one term per layer instead of assuming they match.
                    "attention_count": _as_int(_first(fields, ap, "attention.head_count")),
                    "attention_kv_count": _as_int(_first(fields, ap, "attention.head_count_kv",
                                                           "attention.kv_head_count")),
                    "attention_kv_count_layers": _as_intlist(
                        _first(fields, ap, "attention.head_count_kv",
                               "attention.kv_head_count")),
                    "attention_count_layers": _as_intlist(
                        _first(fields, ap, "attention.head_count")),
                    # Sliding-window layers cache their window, not the whole
                    # context. Without these two the per-layer sum treats 50 of
                    # gemma4's 60 layers as full-context and reports 137 GiB of KV
                    # for a 16 GB model.
                    "sliding_window": _as_int(_first(fields, ap, "attention.sliding_window")),
                    "sliding_window_pattern": _as_boollist(
                        _first(fields, ap, "attention.sliding_window_pattern")),
                    "feed_length": _as_int(_first(fields, ap, "feed_length")),
                    # Capability facts from the tokenizer, not the architecture.
                    #
                    # A GGUF carries its own conversation template, and that is the
                    # only in-file evidence of what the weights were tuned to speak.
                    # It is worth reading for two reasons the header arch cannot
                    # serve: a model with no `tokenizer.chat_template` at all
                    # (Mistral-Code-Architect on this box) is a base/completion model
                    # and will not hold a chat turn no matter how it is prompted; and
                    # the template text itself names the protocols a model was trained
                    # on — `<tool_call>` for tool use, `system` for
                    # role-conditioned chat.
                    #
                    # These are facts about the file, never verdicts. "The template
                    # contains the string <tool_call>" is checkable; "this model is
                    # good at tools" is not, and §4.9 keeps the table on the checkable
                    # side.
                    "has_chat_template": bool(fields.get("tokenizer.chat_template")),
                    "pre_tokenizer": fields.get("tokenizer.ggml.pre") or None,
                    # What the tokenizer does NOT give us, recorded so the next reader
                    # does not re-derive it: role facts cannot come from the
                    # vocabulary on this box.
                    #
                    # `tokenizer.ggml.tokens` is the whole vocabulary — 262144 strings
                    # on gemma-4 here — and when this reader tried to parse it, it
                    # returned a 24-character *string* for a 262144-entry array:
                    # garbage that looked exactly like data. `tokenizer.ggml.token_type`
                    # is parallel and equally large. `tokenizer.ggml.added_tokens`, the
                    # small declared list where special tokens would live, is present on
                    # 0 of the 16 models in this library.
                    #
                    # So no token-derived role column exists. `template_markers` above
                    # carries the role information instead, from a source that is small
                    # (kilobytes of Jinja), present on 15 of 16, and parses correctly.
                    # An always-empty column is not a neutral addition: it renders as a
                    # blank cell that reads as "this model declares nothing", when the
                    # truth is "we have no way to ask".
                    # The template can be large (kilobytes of Jinja). Keep the
                    # signature, drop the body: the index stores facts, not assets.
                    "template_markers": _template_markers(
                        fields.get("tokenizer.chat_template")),
                    # Capability facts, all read from the header. §4.9 is explicit
                    # that the table states facts and a personality states what it
                    # wants — so nothing here is a verdict like "good for coding",
                    # only things that are true of the weights.
                    # Capability facts, read from the header rather than guessed
                    # from the arch name or the filename. §4.9: the table states
                    # facts and a personality says what it wants, so nothing here
                    # is a verdict like "good for coding".
                    #
                    # MoE is `expert_count` > 1 (this box: 256 experts with
                    # expert_used_count 8 on qwen35moe/qwen3next/qwen4exp/laguna).
                    # `expert_used_count` is what makes an MoE a *latency* fact
                    # rather than a size fact — 3B active in a 35B model is a
                    # different machine than 35B dense at the same footprint.
                    "expert_count": _as_int(_first(fields, ap, "expert_count")),
                    "expert_used_count": _as_int(_first(fields, ap,
                                                        "expert_used_count")),
                    "is_moe": _as_bool(_first(fields, ap, "expert_count")) or None,
                    # SSM/recurrent: every qwen35-family export here carries
                    # ssm.conv_kernel and ssm.state_size, including the dense ones,
                    # and none carries ssm.count. So the honest statement is "this
                    # header declares recurrent state", not "this is an SSM model";
                    # the name says which. A future reader that treats True as
                    # "is Mamba" will be wrong about qwen35.
                    "has_ssm_state": bool(_first(fields, ap, "ssm.conv_kernel",
                                                 "ssm.state_size")) or None,
                    # Trained context is above. Vision is a property of the export,
                    # not the arch — a vision model ships an mmproj beside it, which
                    # is why the scanner (not the reader) is the one that can truly
                    # set it. This field is the header's own claim, kept separate so
                    # the two are not confused when they disagree.
                    "has_vision": bool(fields.get("mmproj")) or None,
                    # Params are not stated by any header on this box. Left None
                    # rather than back-computed from file size / quant, because that
                    # would be a quiet second opinion baked into a fact column — and
                    # a quant-aware size estimate is accurate to a factor of ~1.2,
                    # which is not a parameter count. The page renders "—".
                    "raw": fields,
                }
                return out
            finally:
                if isinstance(mm, mmap.mmap):
                    mm.close()
    except (OSError, EOFError, struct.error, ValueError):
        return None


def _as_int(v):
    """Coerce a GGUF numeric KV to int, or None if it is not one.

    int() raises on inf/nan floats, and a single malformed header field must not
    take down the scan of an entire model library."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v)
    except (ValueError, OverflowError):
        return None


def _as_boollist(v) -> list[bool] | None:
    """A GGUF array of booleans, else None. GGUF writers differ on whether a
    boolean array arrives as Python bools or as 0/1 ints, so both are accepted."""
    if not isinstance(v, (list, tuple)) or not v:
        return None
    out = []
    for item in v:
        if isinstance(item, bool):
            out.append(item)
        elif isinstance(item, int) and item in (0, 1):
            out.append(bool(item))
        else:
            return None
    return out


def _as_intlist(v) -> list[int] | None:
    """A GGUF array of numbers as a list of ints, else None.

    Returns None (not []) when the value is a scalar or absent, so callers can
    tell "this arch declares a per-layer value" from "this arch declares one
    number for every layer" — the two need different arithmetic.
    """
    if not isinstance(v, (list, tuple)) or not v:
        return None
    out = []
    for item in v:
        n = _as_int(item)
        if n is None:
            return None
        out.append(n)
    return out


def _as_bool(v):
    """GGUF has no portable boolean in practice: some writers use int 0/1.
    Returns None when absent so callers can tell 'no' from 'not stated'."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return None


# Protocol markers worth reporting out of a chat template. Each entry is
# (regex, label); the regexes are structural — they look for the *shapes* a template
# uses to speak a protocol (`tool_call`, `"role": "tool"`, `{% if tools %}`), not the
# bare word.
#
# The bare-word version of this rule was wrong in the optimistic direction on every
# model in the library: `"tools" in template` matched the Jinja loop variable
# `{% for tool in tools %}` inside a template that never emits a tool call, so 13 of
# 15 models claimed tool support. A template mentions a thing because it renders it,
# which is not the same as the model being trained to produce it — so match the
# delimiters and role strings a template has to spell out literally, and let the
# loop variables go by.

_TEMPLATE_MARKERS: tuple[tuple[re.Pattern, str], ...] = (
    # A tool call the model must emit. The delimiter is `<|...|>` written as a Jinja
    # string literal, so the pattern allows the quote that wraps it. The first
    # version of this had `<\|tool_call|>` with an unescaped alternation inside, which
    # the regex engine read as "`<|tool_call` OR empty string" — it matched the empty
    # string at offset 0 of every template and every model claimed tool support. An
    # alternation of delimiters must escape every pipe that is punctuation.
    (re.compile(r"<\|tool_call\|>|<tool_call>|\[tool_calls?\]", re.IGNORECASE), "tools"),
    # Role comparison, shared by every role marker below.
    #
    # The previous shape anchored the operator directly after the closing quote of
    # the role key. That matches the JSON spelling of a role and misses the Jinja
    # test form, which is what real chat templates actually use: a Jinja conditional
    # closes the key with a bracket BEFORE the comparison operator appears, so an
    # operator anchored right after the quote is never reached and the marker never
    # fires.
    #
    # The cost was silent: tool support and the system role went unreported on every
    # Jinja-templated model in this library. An under-report reads as caution and
    # ships, so the optional bracket belongs in the pattern rather than in an
    # assumption about how templates are written.
    #
    # The operator class accepts colon, single equals, double equals and not-equals;
    # a negative branch still proves the template knows the role exists.
    (re.compile(
        r"role[\"']?\]?\s*[:=!]=?\s*[\"']?tool|\btool_response\b|<\|tool_response\|>",
        re.IGNORECASE), "tool-results"),
    (re.compile(r"<\|think\|>|<thinking>|\bthink\b\s*%", re.IGNORECASE), "thinking"),
    (re.compile(r"role[\"']?\]?\s*[:=!]=?\s*[\"']?system|<\|system\|>", re.IGNORECASE),
     "system"),
    (re.compile(r"role[\"']?\]?\s*[:=!]=?\s*[\"']?user|<\|user\|>|<\|im_start\|>",
                 re.IGNORECASE), "chat"),
    (re.compile(r"\brerank\b|\brank\b\s*(?:passage|document)", re.IGNORECASE), "rerank"),
    (re.compile(r"\b(?:query|document)_(?:prefix|passage)\b|<\|embed\|?>", re.IGNORECASE),
     "embedding"),
)


def _template_markers(template) -> list[str]:
    """Which conversation protocols a chat template actually emits, as short labels.

    The template is the contract the weights were trained against, so the tags a
    template must spell out literally are evidence the model speaks that protocol.
    Structural matching (delimiters, `"role": "x"`) rather than substring matching,
    because templates are Jinja full of loop variables named `tools`, `messages` and
    `system` that say nothing about what the model was trained to do.

    Returns [] for a missing template — itself the notable case (a base model), which
    the caller labels instead of leaving blank.
    """
    if not isinstance(template, str) or not template.strip():
        return []
    found: set[str] = set()
    for pattern, label in _TEMPLATE_MARKERS:
        if pattern.search(template):
            found.add(label)
    return sorted(found)


def _first(fields: dict, prefix: str, *keys: str):
    """First present key, each tried both with and without the arch prefix.

    Architectures are inconsistent about namespacing: `qwen3next` writes
    `full_attention_interval` unprefixed in some exports and prefixed in others,
    and a missing value silently degrades a memory estimate to nonsense rather
    than failing loudly. Trying both spellings is cheaper than being wrong.
    """
    for k in keys:
        for cand in (prefix + k, k):
            if cand in fields:
                return fields[cand]
    return None


# ------------------------------------------------------------- safetensors
def sniff_safetensors(path) -> dict | None:
    """Read only the JSON header (8-byte length prefix) — no weights.

    Returns {format, num_tensors, keys (capped), metadata} or None."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(8)
            if len(head) < 8:
                return None
            (hlen,) = struct.unpack("<Q", head)
            if hlen > 64 * 1024 * 1024:  # a header that big is not a header
                return None
            hdr = json.loads(f.read(hlen).decode("utf-8", "replace"))
        keys = [k for k in hdr if k != "__metadata__"]
        return {
            "format": "safetensors",
            "num_tensors": len(keys),
            "keys": keys[:400],
            "metadata": hdr.get("__metadata__") or {},
        }
    except (OSError, ValueError, json.JSONDecodeError, struct.error):
        return None


def sniff_file(path) -> dict | None:
    """Try both formats; first magic that matches wins."""
    return sniff_gguf(path) or sniff_safetensors(path)


# ------------------------------------------------- ComfyUI category rules
def classify_safetensors(keys: list[str], size_bytes: int = 0) -> str | None:
    """Which ComfyUI ``models/<category>/`` folder a safetensors file belongs
    to, from its tensor keys (the same signals ComfyUI's own loaders use).

    Returns None when unsure — the caller then asks the user.
    """
    ks = keys or []
    if not ks:
        return None
    joined = set(k.split(".")[0] for k in ks)

    if any(k.startswith("lora_") or ".lora_" in k for k in ks):
        return "loras"
    # VAEs: encoder/decoder mid blocks, no diffusion weights
    if ("encoder" in joined and "decoder" in joined) or \
       any(k.startswith(("encoder.mid.", "decoder.mid.")) for k in ks):
        return "vae"
    # CLIP / text encoders
    if joined & {"cond_stage_model", "conditioner"} or \
       any(k.startswith(("transformer.text_model.", "text_model.")) for k in ks):
        return "clip" if any("clip" in k or "cond_stage" in k for k in ks) else "text_encoders"
    # Text embeddings: a handful of tensors, usually one
    if len(ks) <= 4 and any(
        k in ("input_embedding", "emb", "embeddings") or k.endswith("_emb")
        for k in ks):
        return "embeddings"
    # SD3/Flux-style diffusion models -> diffusion_models; SD/SDXL checkpoints
    # wrap their weights under model.diffusion_model.* -> checkpoints
    if any(k.startswith("diffusion_model.") for k in ks):
        return "diffusion_models"
    if any(k.startswith("model.diffusion_model.") for k in ks):
        return "checkpoints"
    # Esrgan-style upscalers: small conv stacks, 'model.N.weight' pattern
    if len(ks) < 64 and any(k.startswith("model.") and ".weight" in k for k in ks) \
       and size_bytes and size_bytes < 200 * 1024 * 1024:
        return "upscale_models"
    if any(k.startswith("model.") for k in ks):
        return "checkpoints"
    return None
