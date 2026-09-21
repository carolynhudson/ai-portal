"""Read real tokenizer.chat_template strings from this box's GGUFs.

Reuses the shape of modelmeta.sniff_gguf's KV walk (mmap + skip-array-length, never
materialise a 262144-string vocabulary array) because a naive reader Materializes
tokenizer.ggml.tokens and dies with MemoryError -- which is exactly what the first
version of this probe did.
"""
from __future__ import annotations

import contextlib
import mmap
import pathlib
import re
import struct
import sys

sys.path.insert(0, "/home/cortana/ai-projects/ai-portal")

LIB = pathlib.Path.home() / "ai/models/llama"

# GGUF scalar types we can just read; anything else is skipped by size.
STR, ARRAY = 10, 11


def chat_template(path: pathlib.Path) -> str:
    """The chat_template string, or "" for anything unreadable.

    Every failure mode here returns "" rather than raising: this is a diagnostic that
    walks a library of other people's GGUFs, and a truncated or non-GGUF file is a fact
    about that file, not a reason to abort the audit.
    """
    try:
        with open(path, "rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return ""
    # Bound before the try: the except path below returns this, and a name assigned
    # only inside the try is "possibly unbound" at the handler.
    want = ""
    try:
        if mm[:4] != b"GGUF":
            return ""
        # header: magic(4) version(4) n_tensors(8) n_kv(8)
        pos = 4 + 4
        pos += 8                      # n_tensors: not needed, the KV walk is self-delimiting
        (n_kv,) = struct.unpack_from("<Q", mm, pos)
        pos += 8

        def rd_str(p: int) -> tuple[str, int]:
            (ln,) = struct.unpack_from("<Q", mm, p)
            p += 8
            return mm[p:p + ln].decode("utf-8", "replace"), p + ln

        for _ in range(n_kv):
            key, pos = rd_str(pos)
            (t,) = struct.unpack_from("<I", mm, pos)
            pos += 4
            if t == STR:
                val, pos = rd_str(pos)
                if key == "tokenizer.chat_template":
                    want = val
                continue
            if t == ARRAY:
                (arr_t,) = struct.unpack_from("<I", mm, pos)
                (arr_n,) = struct.unpack_from("<Q", mm, pos + 4)
                pos += 12
                # Skip arrays by computing their byte size. Materialising a 262144-entry
                # string array is what makes this probe die instead of answer.
                elem = {STR: None, 0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4,
                        6: 4, 7: 8, 8: 8, 9: 8}.get(arr_t)
                if elem is None:
                    # array of arrays or unknown: cannot cheaply skip
                    return want
                if arr_t == STR:
                    for _ in range(min(arr_n, 300000)):
                        (ln,) = struct.unpack_from("<Q", mm, pos)
                        pos += 8 + ln
                else:
                    pos += arr_n * elem
                continue
            size = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 8, 8: 8, 9: 8}.get(t)
            if size is None:
                return want
            pos += size
        return want
    except (struct.error, IndexError, BufferError, MemoryError):
        # A header that runs off the end of the file, or an array size that does not
        # match reality. Report what was found up to that point.
        return want
    finally:
        # Nothing to recover from a failing close: the read is over and the value has
        # already been returned. Logging it would be noise on a diagnostic whose whole
        # job is to tolerate odd files.
        with contextlib.suppress(OSError, BufferError):
            mm.close()


def main() -> int:
    names = sys.argv[1:] or sorted(
        d.name for d in LIB.iterdir() if d.is_dir())
    for name in names:
        d = LIB / name
        mains = [p for p in sorted(d.glob("*.gguf"))
                 if not p.name.lower().startswith(("mmproj", "mtp-", "draft-", "imatrix"))]
        if not mains:
            continue
        tmpl = chat_template(mains[0])
        print("=" * 76)
        print(f"{name}  (template {len(tmpl)} chars)")
        if not tmpl:
            print("  NO chat_template")
            continue
        print("  tool vocabulary:")
        for probe in ("tool", "Tool", "TOOL", "function", "Function", "plugin",
                      "<tool_call>", "<function", "<tool_call", "tool_name",
                      "tool_response", "tools"):
            n = tmpl.count(probe)
            if n:
                print(f"    {probe!r:16s} x{n}")
        print("  reasoning vocabulary:")
        for probe in ("think", "thinking", "THINK", "reason", "<|think", "<think",
                      "analysis", "cot"):
            n = tmpl.count(probe)
            if n:
                print(f"    {probe!r:12s} x{n}")
        roles = sorted(set(re.findall(r"role[\"']?\]?\s*[:=!]=+\s*[\"']([a-zA-Z_]+)", tmpl)))
        dels = sorted(set(re.findall(r"<\|([a-zA-Z_]+)\|>", tmpl)))
        print("  role comparisons:", roles)
        print("  <|delimiters|>:", dels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
