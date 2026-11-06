#!/usr/bin/env python3
"""Cache→library migration for llama router models — the operator's CLI.

    tools/modelmigrate.py report                 # every cache model, its verdict
    tools/modelmigrate.py report --id org/r:Q4_K_M
    tools/modelmigrate.py apply --id org/r:Q4_K_M [--yes]

Report is the default and runs first by standing rule (the §4.10.5 incident):
read the plan, then apply. One model per apply; the library and cache law
lives in app/core/modelmigrate.py — this file only speaks to it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import db, modelmigrate  # noqa: E402
from app.core import modelindex  # noqa: E402


def _split(mid: str) -> tuple[str, str]:
    repo, _, tag = mid.rpartition(":")
    if not repo or not tag:
        raise SystemExit(f"model id should look like org/repo:TAG, got {mid!r}")
    return repo, tag


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["report", "apply"], nargs="?",
                    default="report")
    ap.add_argument("--id", help="cache model id (org/repo:TAG)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation (for scripted use)")
    ap.add_argument("--rescan", action="store_true",
                    help="rebuild the model index before reporting")
    a = ap.parse_args()
    db.init()
    if a.rescan:
        modelindex.rescan()

    if a.cmd == "report":
        flags = modelmigrate.migratable()
        rows = [m for m in modelindex.rows() if m.get("source") == "cache"]
        if a.id:
            repo, tag = _split(a.id)
            rows = [m for m in rows
                    if str(m["id"]).rpartition(":")[0] == repo
                    and str(m["id"]).rpartition(":")[2].upper() == tag.upper()]
        total = 0
        for m in sorted(rows, key=lambda r: str(r["id"])):
            mid = str(m["id"])
            f = flags.get(mid, {"ok": False, "reason": "not in the index"})
            gb = (m.get("bytes") or 0) / 2**30
            total += gb
            mark = "→ " + f["dir"] if f["ok"] else "·  " + (f.get("reason")
                                                             or "not migratable")
            print(f"{gb:8.1f} GiB  {mid:58} {mark}")
        print(f"{len(rows)} cache model(s), {total:.0f} GiB; "
              f"{sum(1 for m in rows if flags.get(str(m['id']), {}).get('ok'))}"
              " migratable")
        return

    if not a.id:
        raise SystemExit("apply needs --id org/repo:TAG")
    repo, tag = _split(a.id)
    plan = modelmigrate.plan_for(repo, tag)
    print(f"plan: {plan['id']} → {plan['dest']}/")
    for f in plan["files"]:
        ex = f"  [{f['exists']}]" if f.get("exists") else ""
        print(f"  {f['mode']:4} {f['role']:12} {f['name']}"
              f"  {f['size'] / 2**20:.0f} MiB{ex}")
    problems = modelmigrate.preflight(plan)
    if problems:
        print("PREFLIGHT REFUSED:")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)
    if not a.yes:
        if input(f"migrate {plan['id']} ? [y/N] ").strip().lower() != "y":
            raise SystemExit("declined")
    result = modelmigrate.apply(plan)
    print(f"migrated {result['id']} → {result['dest_name']}/")
    if result.get("refs", {}).get("rewrote"):
        print("references rewritten:",
              ", ".join(result["refs"]["rewrote"]))
        print("→ open the Personalities page and apply so presets.ini follows")


if __name__ == "__main__":
    main()
