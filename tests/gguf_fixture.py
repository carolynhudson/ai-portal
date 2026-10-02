"""GGUF fixtures the index and the bench tool both read — and nothing else.

This lives outside any test module on purpose. A test file cannot import another
test file: `test_modelindex.py` claims `AI_PORTAL_PASSWORD` (and a throwaway
`AI_PORTAL_HOME`) with `setdefault` at import, so importing it from a module that
collects earlier wins that race for the whole session, and every later module
logs in with the password it *thinks* it set. Measured: 56 failures and 12 errors
(401s across test_hero, test_ui_contract, test_comfyui and others) from one line
in a test file that only wanted a GGUF writer.

Written by hand rather than with a GGUF library so a change in the portal's
reader cannot make these fixtures vacuously pass.
"""

from __future__ import annotations

import struct
from pathlib import Path

# The reader's type numbers, not the current spec's — see test_modelindex.
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
    sniff_gguf and nothing else. Keys are prefixed with `testarch.` because that
    is how the reader looks up block_count &c."""
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
    up without a prefix. Bumping kv_count keeps the file parseable: a reader that
    loops `for _ in range(kv_count)` never sees an entry the header does not count.
    """
    # Header is magic(4) version(4) tensor_count(8) kv_count(8) = 24 bytes, with
    # kv_count at offset 16. Append "name + value" and bump the count by one.
    raw = bytearray(path.read_bytes())
    n = struct.unpack_from("<Q", raw, 16)[0]
    struct.pack_into("<Q", raw, 16, n + 1)
    path.write_bytes(bytes(raw) + _enc_str(key) + _enc_value(bool(value)))


def _gguf_with_tensors(path: Path, names: list[str], kv: dict | None = None) -> None:
    """Fixture GGUF whose tensor-info section names `names`. The KV-only writer
    emits no tensor infos, so a reader that walks tensor *names* (the draft-head
    probe) needs this one. `kv` lets a test state the architecture fields the same
    file declares, so "the header asks for a draft layer" and "the weights carry
    one" can be tested against each other instead of only in the field.
    """
    _write_fake_gguf(path, kv if kv is not None else {"block_count": 4})
    raw = bytearray(path.read_bytes())
    struct.pack_into("<Q", raw, 8, len(names))            # tensor_count
    info = b""
    for n in names:
        info += (_enc_str(n) + struct.pack("<I", 2)
                 + struct.pack("<QQ", 8, 8) + struct.pack("<I", 10)
                 + struct.pack("<Q", 0))
    path.write_bytes(bytes(raw) + info)


def _write_raw_gguf(path: Path, pairs: list[tuple[str, object]]) -> None:
    """GGUF whose keys are written exactly as given, unprefixed.

    _write_fake_gguf namespaces everything under the architecture because that is
    how arch fields are looked up, but the tokenizer arrays GGUF defines —
    `tokenizer.ggml.tokens`, `tokenizer.ggml.added_tokens` — are top-level, and a
    capability reader that only ever saw prefixed keys could never test them.
    """
    hdr = (struct.pack("<4s", b"GGUF") + struct.pack("<I", 3)
           + struct.pack("<Q", 0) + struct.pack("<Q", len(pairs)))
    assert len(hdr) == 24, f"KV section must start at offset 24, header is {len(hdr)}"
    out = bytearray()
    for key, value in pairs:
        out += _enc_str(key) + _enc_value(value)
    path.write_bytes(hdr + bytes(out))


