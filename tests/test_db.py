"""db: the state-agnostic law - the schema exists when the file opens.

The failure this pins (2026-11-18): the harness seed lane read settings
in a standalone run and died on "no such table: settings" - it worked
in the full suite only because an earlier file had built the app (which
runs init()). Order-dependency is a flake on a schedule: every lane
(CLI, scripts, tests) gets a usable schema from the first connect.
"""
import os
import sqlite3
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-db-"))

from app.core import db  # noqa: E402


def test_first_touch_reads_work_without_an_explicit_init(tmp_path, monkeypatch):
    # a pristine home: no db file, no init() call by anyone
    monkeypatch.setattr(db, "DB", tmp_path / "first-touch.db")
    assert db.get_setting("no-such-key", "default") == "default"
    # and the write lanes work too
    db.set_setting("pinned-by-this-test", {"ok": True})
    assert db.get_setting("pinned-by-this-test") == {"ok": True}


def test_init_is_idempotent_per_connection(tmp_path, monkeypatch):
    # the workroom lesson: a PRIVATE connection swapped in under an
    # already-initialized module must still get its schema - the guard
    # is the connection's user_version, not a module flag
    monkeypatch.setattr(db, "DB", tmp_path / "private.db")
    db._local.conn = None
    conn = sqlite3.connect(str(tmp_path / "private.db"))
    conn.row_factory = sqlite3.Row
    db._local.conn = conn
    db.init()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    db.init()  # second call: schema already in place, no re-run
    db._local.conn = None
