#!/usr/bin/env python3
"""Migrate a local secret file into the mesh hub (one-shot tool).

Law: copy+verify before anything is retired. This tool NEVER deletes
the source file; it puts, reads back, and compares digests. The hub
holds only age ciphertext (meshctl encrypts client-side).

usage: mesh_migrate.py <secret-name> <path> [recipients-csv]
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import meshhub  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    name, src = sys.argv[1], sys.argv[2]
    recips = [r for r in sys.argv[3].split(",") if r] if len(sys.argv) > 3 else []
    data = Path(src).read_bytes()
    meshhub.put(name, data, recipients=recips)
    back = meshhub.get(name)
    a, b = hashlib.sha256(data).hexdigest(), hashlib.sha256(back).hexdigest()
    if a != b:
        print(f"MISMATCH name={name} src={a[:12]} hub={b[:12]} -- source left in place")
        return 1
    print(f"migrated name={name} bytes={len(data)} sha256={a[:12]} recipients={recips or '(owner only)'}")
    print(f"source file untouched at {src} -- retire manually after agent cutover")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
