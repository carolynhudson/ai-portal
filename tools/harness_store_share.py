#!/usr/bin/env python3
"""Share the opencode session store between the portal's harness host and manual `opencode`.

Owner Oct-18: sessions launched from the Projects page lived in the harness's isolated
data home (`XDG_DATA_HOME=~/ai/state/harness/host-4096/data`), so `opencode` started in
the project folder showed none of them. The isolation's reason (a v1 host sharing a db
with a v2 CLI: the v2 migration dropped `project_id` and broke the v1 server's routes,
ref=err_4c90c294) is retired — the harness is a v2 binary and so is the CLI. One shared
v2 store (~/.local/share/opencode) is the supported shape.

This tool merges the harness store INTO the default store, so old portal sessions arrive
with the manual ones (it does not overwrite the manual store):

  1. refuse to run while the harness host answers (stop ai-harness first),
  2. require the same schema migration SET on both dbs,
  3. back both stores up,
  4. ATTACH the harness db and insert per table with a NAMED COLUMN LIST — measured on
     this box the two dbs order `session_v2`'s columns differently (the migration that
     appends columns appends them at different times), and `INSERT ... SELECT *` into a
     differently-ordered table writes every value into the wrong column. Named columns
     merge by name, and `INSERT OR IGNORE` is honest: ids are content ids (ses_*, msg_*,
     project hashes) and a collision is the same row.
  5. event_sequence is per aggregate: take the max seq, never a blind copy.
  6. merge the storage directories (snapshot/, tool-output/, repos/, shell/) by moving
     entries the default store does not have — names are ids.

Dry-run by default; --apply writes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
import urllib.request
from pathlib import Path

DEFAULT_DATA = Path.home() / ".local" / "share" / "opencode"
HARNESS_DATA = Path.home() / "ai" / "state" / "harness" / "host-4096" / "data" / "opencode"
PORT = 4096
SKIP_TABLES = {"migration"}          # schema ledger: not data, never merged
STORAGE_DIRS = ("snapshot", "tool-output", "repos", "shell")


def host_answering() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/global/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def tables(db: sqlite3.Connection) -> list[str]:
    return [r[0] for r in db.execute("select name from sqlite_master where type='table'")]


def columns(db: sqlite3.Connection, t: str) -> list[str]:
    return [r[1] for r in db.execute(f"pragma table_info('{t}')")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    args = ap.parse_args()

    src_db = HARNESS_DATA / "opencode.db"
    dst_db = DEFAULT_DATA / "opencode.db"
    for p, who in ((src_db, "harness"), (dst_db, "default")):
        if not p.exists():
            print(f"FAIL: the {who} store is not at {p}")
            return 1
    if host_answering():
        print(f"FAIL: a harness host answers on :{PORT} — the store is its open file.")
        print("      stop it first:  systemctl --user stop ai-harness")
        return 1

    print(f"src (harness): {src_db}")
    print(f"dst (default): {dst_db}")

    s = sqlite3.connect(src_db)
    s.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold the WAL in before attaching
    d = sqlite3.connect(dst_db)

    mig_s = {r[0] for r in s.execute("select id from migration")}
    mig_d = {r[0] for r in d.execute("select id from migration")}
    if mig_s != mig_d:
        print("FAIL: the two stores are at different schema migrations:")
        print(f"      only-in-harness: {sorted(mig_s - mig_d)}")
        print(f"      only-in-default: {sorted(mig_d - mig_s)}")
        print("      run the same opencode version against both (opening each once applies")
        print("      its migrations), then re-run this tool.")
        return 1

    shared = sorted(set(tables(s)) & set(tables(d)))
    only_s = sorted(set(tables(s)) - set(tables(d)))
    if only_s:
        print(f"note: harness-only tables skipped: {only_s}")

    plan: dict[str, list[str]] = {}
    for t in shared:
        if t in SKIP_TABLES:
            continue
        cols_d, cols_s = columns(d, t), columns(s, t)
        cols = [c for c in cols_d if c in cols_s]
        dropped = [c for c in cols_s if c not in cols_d]
        if dropped:
            print(f"note: {t}: harness columns absent from default, skipped: {dropped}")
        plan[t] = cols

    for t in plan:
        n = s.execute(f'select count(*) from "{t}"').fetchone()[0]  # noqa: S608
        m = d.execute(f'select count(*) from "{t}"').fetchone()[0]  # noqa: S608
        print(f"  {t:22s} harness={n:<7d} default={m}")

    if not args.apply:
        print("\ndry-run: nothing written. Re-run with --apply.")
        return 0

    for p in (dst_db, src_db):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(p, p.with_name(p.name + f".premerge-{stamp}"))
        print(f"backup: {p.with_name(p.name + f'.premerge-{stamp}')}")

    d.execute("ATTACH DATABASE ? AS src", (str(src_db),))
    for t, cols in plan.items():
        collist = ", ".join(f'"{c}"' for c in cols)
        d.execute(f'INSERT OR IGNORE INTO main."{t}" ({collist}) '  # noqa: S608
                  f'SELECT {collist} FROM src."{t}"')
        print(f"merged {t}: +{d.execute('select changes()').fetchone()[0]} rows")
    if "event_sequence" in plan:
        d.execute(
            "INSERT OR IGNORE INTO main.event_sequence (aggregate_id, seq, owner_id) "
            "SELECT aggregate_id, seq, owner_id FROM src.event_sequence")
        d.execute(
            "UPDATE main.event_sequence SET seq = "
            "(SELECT max(sq.seq) FROM src.event_sequence sq WHERE sq.aggregate_id = "
            "main.event_sequence.aggregate_id AND sq.seq > main.event_sequence.seq) "
            "WHERE aggregate_id IN (SELECT aggregate_id FROM src.event_sequence)")
        print("event_sequence: aggregates merged, seqs taken as max")
    d.commit()
    d.execute("DETACH DATABASE src")
    d.close()
    s.close()

    for sub in STORAGE_DIRS:
        src, dst = HARNESS_DATA / sub, DEFAULT_DATA / sub
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        moved = 0
        for entry in src.iterdir():
            target = dst / entry.name
            if target.exists():
                continue
            shutil.move(str(entry), str(target))
            moved += 1
        print(f"storage {sub}: +{moved} entries")

    print("\ndone. Start the harness (systemctl --user start ai-harness) and open `opencode`")
    print("in a project folder: the sessions it launched are now in one store.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
