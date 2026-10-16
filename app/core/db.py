"""SQLite persistence for the portal (WAL mode), at ``~/ai/state/portal/portal.db``.

Zero-dependency (stdlib sqlite3). The schema covers the full portal lifecycle
(tools, releases, jobs, downloads, model inventory, presets history, audit
events); Phase A uses a slice of it and later phases fill the rest in.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time

from . import logsetup, paths

DB = paths.PORTAL_STATE / "portal.db"
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
  key TEXT PRIMARY KEY, name TEXT, kind TEXT, installed TEXT, deployed TEXT,
  service_name TEXT, service_state TEXT, healthy INTEGER, detail TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS releases (
  tool TEXT, id TEXT, path TEXT, "commit" TEXT, sdk TEXT, created TEXT, active INTEGER,
  PRIMARY KEY (tool, id)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, target TEXT, status TEXT, log TEXT, started_at REAL, ended_at REAL,
  -- who wrote this row, so "running" can be proved dead without waiting for a
  -- timeout: a row from an earlier boot, or from a process that is gone, is not
  -- a job, whatever it says
  pid INTEGER, boot TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT, files TEXT, placement TEXT, status TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY, dir TEXT, role_files TEXT, size_bytes INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS model_files (
  path TEXT PRIMARY KEY, model TEXT, size INTEGER, sha256 TEXT, role TEXT,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS presets_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, note TEXT, before TEXT, after TEXT
);
-- Model configs and custom personalities (§4.7). Both are definitions the portal *shows*
-- and validates; presets.ini remains the only thing the router reads.
--
-- `model_configs.name` is not a display label: render() writes it into the base model's own
-- section as `alias = <name>`, and the router then serves and routes by it (measured, b10964:
-- `/models` lists the alias and a chat request naming it returns 200). That is also why a name is
-- a Primary key and not a per-personality field -- the router keeps aliases in one namespace across
-- all models, and a collision makes it refuse to start at all.
CREATE TABLE IF NOT EXISTS model_configs (
  name TEXT PRIMARY KEY, base_model TEXT NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
  created_at REAL, updated_at REAL
);
-- `models` is a JSON list of model config names and/or library model ids; NULL means the whole
-- library, which is what `default` means and why it stays computed rather than stored.
CREATE TABLE IF NOT EXISTS personalities (
  name TEXT PRIMARY KEY, models_max INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT '', models TEXT,
  global TEXT NOT NULL DEFAULT '{}',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
-- Projects (§6.14.1n). `path` is the realpath, so a folder the operator adopted and a folder the
-- portal created are the same row shape. `notes` is JSON: which init steps ran, which were refused.
CREATE TABLE IF NOT EXISTS projects (
  path TEXT PRIMARY KEY, name TEXT, created REAL, last_opened REAL, notes TEXT
);
-- Agent memory (RAG-for-opencode): what harness sessions record for the sessions that follow
-- them. The store is the portal's — the same sqlite base, the same audit trail — and the harness
-- reaches it only through the MCP server (app/core/kb_mcp.py), never through the browser.
-- FTS5 is the recall: measured available on this box's python 3.14 sqlite.
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', created_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  text, tags, content='memory', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
  INSERT INTO memory_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        paths.PORTAL_STATE.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    c = _conn()
    c.executescript(SCHEMA)
    # Pre-existing DBs: add each column once. An existing column is the normal case here, not an
    # exception worth a name — SQLite raises `duplicate column name` and we continue.
    with contextlib.suppress(sqlite3.OperationalError):
        c.execute("ALTER TABLE model_files ADD COLUMN meta TEXT")
    for col in ("pid INTEGER", "boot TEXT"):
        with contextlib.suppress(sqlite3.OperationalError):
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    # Memory v2 (RAG): the embedding rides the row — JSON floats, the sidecar's 768
    # dims. A column, not a side table: an embedding without its memory (or the
    # reverse) is exactly the orphaned-index failure the vector stores are famous for.
    with contextlib.suppress(sqlite3.OperationalError):
        c.execute("ALTER TABLE memory ADD COLUMN embedding TEXT")
    c.commit()


def upsert_tool(row: dict) -> None:
    c = _conn()
    c.execute(
        """INSERT INTO tools (key,name,kind,installed,deployed,service_name,
                              service_state,healthy,detail,updated_at)
           VALUES (:key,:name,:kind,:installed,:deployed,:service_name,
                   :service_state,:healthy,:detail,:updated_at)
           ON CONFLICT(key) DO UPDATE SET
             name=excluded.name, kind=excluded.kind, installed=excluded.installed,
             deployed=excluded.deployed, service_name=excluded.service_name,
             service_state=excluded.service_state, healthy=excluded.healthy,
             detail=excluded.detail, updated_at=excluded.updated_at""",
        row,
    )
    c.commit()


def event(actor: str, kind: str, detail: str = "") -> None:
    """Audit-log one mutation.

    Every state-changing action goes through here, so the same line also lands
    in the rotating file log (``~/ai/logs/portal.log``): the DB answers "what
    happened, queryable", the file answers "what happened around the time the
    router fell over", including whatever the code logged in between.
    """
    logsetup.get("audit").info("%s %s: %s", actor, kind, detail)
    c = _conn()
    c.execute("INSERT INTO events (ts, actor, kind, detail) VALUES (?,?,?,?)",
              (time.time(), actor, kind, detail))
    c.commit()


# ------------------------------------------------------------------ settings
# Tunable values the future settings/admin page will expose. Each has a safe
# default so the portal works before anything is stored; stored overrides win.
SETTINGS_DEFAULTS = {
    "router_poll_ms": 2500,    # dashboard hero (llama router) refresh interval
    "metrics_poll_ms": 2000,   # host metrics chart refresh interval
    "router_metrics": True,    # llama router runs with --metrics (exposes /metrics)
    "remote_check_ttl_hours": 6.0,  # upstream probe cache (a few checks a day)
    # Where the coding projects live. Validated by `portalsettings`, never by the page: this is
    # the setting that decides where an agent gets to write files (§6.14.1n).
    "projects_root": "~/ai-projects",
}


def get_setting(key: str, default=None):
    c = _conn()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key: str, value) -> None:
    c = _conn()
    c.execute("INSERT INTO settings (key,value) VALUES (?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value)))
    c.commit()
    event("portal", "settings", f"{key}={json.dumps(value)}")


def current_settings() -> dict:
    """Defaults overlaid with any stored overrides."""
    out = dict(SETTINGS_DEFAULTS)
    for k in SETTINGS_DEFAULTS:
        v = get_setting(k)
        if v is not None:
            out[k] = v
    return out


# ------------------------------------------------------------ generic access
# Thin helpers so background threads (updater jobs) can touch the DB without
# reaching into the connection internals.

def execute(sql: str, params: tuple = ()) -> None:
    c = _conn()
    c.execute(sql, params)
    c.commit()


def query(sql: str, params: tuple = ()) -> list:
    return _conn().execute(sql, params).fetchall()


# ------------------------------------------------------------------ releases

def record_release(tool: str, rid: str, path: str, commit: str, sdk: str,
                   active: bool = False) -> None:
    execute(
        '''INSERT INTO releases (tool, id, path, "commit", sdk, created, active)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(tool,id) DO UPDATE SET
             path=excluded.path, "commit"=excluded."commit", sdk=excluded.sdk,
             active=excluded.active''',
        (tool, rid, path, commit, sdk, time.time(), 1 if active else 0))


def set_release_active(tool: str, rid: str, active: bool = True) -> None:
    execute('UPDATE releases SET active=? WHERE tool=? AND id=?',
            (1 if active else 0, tool, rid))


# -------------------------------------------------------------------- models

def index_model(model_id: str, dir_path, role: str, files: list) -> None:
    """Upsert a model into the portal index (``models`` + ``model_files``).

    ``files`` is a list of {"name" (relative to dir_path), "size", "sha256",
    "role", "meta"}; sha256 may be None for big pre-existing files not
    hashed yet, meta is the sniffed header dict (JSON) or None.
    """
    from pathlib import Path as _Path

    c = _conn()
    c.execute(
        "INSERT INTO models (id, dir, role_files, size_bytes, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "dir=excluded.dir, role_files=excluded.role_files, "
        "size_bytes=excluded.size_bytes, updated_at=excluded.updated_at",
        (model_id, str(dir_path),
         json.dumps({"role": role, "files": [f["name"] for f in files]}),
         sum(f["size"] for f in files), time.time()))
    for f in files:
        c.execute(
            "INSERT INTO model_files (path, model, size, sha256, role, meta) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "model=excluded.model, size=excluded.size, "
            "sha256=excluded.sha256, role=excluded.role, meta=excluded.meta",
            (str(_Path(dir_path) / f["name"]), model_id, f["size"],
             f.get("sha256"), f.get("role", role),
             json.dumps(f["meta"]) if f.get("meta") else None))
    c.commit()
