#!/usr/bin/env python3
"""The model index, at the command line (PORTAL_PLAN §6.14.1zl).

    tools/modelindex.py status          # rows by source, provenance census, last scan
    tools/modelindex.py show <id>       # one row, every column (the card's data)
    tools/modelindex.py rescan          # rebuild the table; prints problems
    tools/modelindex.py tree            # the models page's grouping, in text

The portal's models page reads the table; this tool is the operator's eye on
the same table. Rescan is the same call the page's POST /api/models/rescan
fires as a job — one indexer, two doors.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import db, modelindex  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["status", "show", "rescan", "tree"],
                    nargs="?", default="status")
    ap.add_argument("id", nargs="?", default="")
    a = ap.parse_args()
    db.init()

    if a.cmd == "rescan":
        r = modelindex.rescan()
        print(f"scanned {r['models']} model(s) — {r['library']} from the library, "
              f"{r['cache']} from the hub cache")
        for p in r.get("problems", []):
            print(f"  ! {p.get('kind')}: {p.get('path', '')} {p.get('note', '')}")
        return

    rows = modelindex.rows()
    if a.cmd == "show":
        for m in rows:
            if str(m["id"]) == a.id:
                for k in sorted(m):
                    if m[k] not in (None, "", 0, []):
                        print(f"{k:>22}: {m[k]}")
                return
        raise SystemExit(f"no row {a.id!r} ({len(rows)} rows)")

    if a.cmd == "tree":
        # The page's grouping law, in text: quantizer / model / quant, from
        # the id, the provenance ledger, or the header's own words.
        for m in sorted(rows, key=lambda r: str(r["id"])):
            mid = str(m["id"])
            repo, _, rest = mid.rpartition("/")
            stem, _, tag = rest.rpartition(":")
            if not repo:
                repo = m.get("hf_repo") or "Unknown"
                stem = stem or stem_of(m)
            print(f"{repo:52} {stem:44} {m.get('quant') or tag or 'Unknown':10} [{m['source']}]")
        return

    lib = [m for m in rows if m.get("source") == "library"]
    cache = [m for m in rows if m.get("source") == "cache"]
    prov = [m for m in lib if m.get("hf_repo")]
    print(f"{len(rows)} rows: {len(lib)} library ({len(prov)} with HF provenance), "
          f"{len(cache)} cache")
    for m in lib:
        print(f"  {str(m['id']):56} "
              f"{(m.get('hf_repo') or 'no HF identity')}"
              f"{(':' + str(m['hf_tag'])) if m.get('hf_tag') else ''}")


def stem_of(m: dict) -> str:
    """The tree's metadata fallback: general.name, else the id's own stem."""
    return str(m.get("name") or "").strip() or str(m["id"]).rsplit("-", 1)[0]


if __name__ == "__main__":
    main()
