#!/usr/bin/env python3
"""Print the capability facts the index derives for every model in the library.

A diagnostic, not a test: it prints what the reader actually found in each GGUF so a
disagreement between "what the page shows" and "what I think the model is" can be
resolved against the file rather than argued about.

    python tools/capability_audit.py [--models DIR]

Read-only. It never writes to portal.db and never rescans the live index.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The portal expects a home; use a throwaway one so this cannot touch a real DB.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp())

from app.core import modelmeta  # noqa: E402

_AUX = ("mmproj", "mtp-", "draft-", "imatrix", "adapter", "lora", "vocab", "tokenizer")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=str(Path.home() / "ai/models/llama"))
    args = ap.parse_args()

    root = Path(args.models)
    if not root.is_dir():
        print(f"no such directory: {root}")
        return 1

    print(f"{'model':33s} {'arch':12s} {'tpl':3s} {'tools':5s} markers / declared roles")
    counts = {"tools": 0, "chat": 0, "thinking": 0, "system": 0}
    n = 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        gguf = next((p for p in d.glob("*.gguf")
                     if not p.name.lower().startswith(_AUX)), None)
        if gguf is None:
            continue
        meta = modelmeta.sniff_gguf(gguf) or {}
        n += 1
        mk = meta.get("template_markers") or []
        roles = meta.get("token_types") or []
        for k in counts:
            counts[k] += k in mk
        print(f"{d.name[:33]:33s} {str(meta.get('arch') or '?')[:12]:12s} "
              f"{'y' if meta.get('has_chat_template') else 'N':3s} "
              f"{'yes' if 'tools' in mk else '-':5s} "
              f"{','.join(mk) or '(none)':32s} {','.join(roles) or '-'}")

    print(f"\n{n} model(s); markers: {counts}")
    print("facts from the file, never verdicts — a missing marker means the file does")
    print("not declare it, not that the model cannot do it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
