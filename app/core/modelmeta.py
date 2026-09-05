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
                    "name": fields.get("general.name") or None,
                    "size_label": fields.get("general.size_label") or None,
                    "context_length": _as_int(fields.get(ap + "context_length")),
                    "tensor_count": tensor_count,
                    "organization": fields.get("general.organization") or None,
                    "quantized_by": fields.get("general.quantized_by") or None,
                }
                return out
            finally:
                if isinstance(mm, mmap.mmap):
                    mm.close()
    except (OSError, EOFError, struct.error, ValueError):
        return None


def _as_int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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
