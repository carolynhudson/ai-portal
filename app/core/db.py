"""SQLite persistence for the portal (WAL mode), at ``~/ai/state/portal/portal.db``.

Zero-dependency (stdlib sqlite3). The schema covers the full portal lifecycle
(tools, releases, jobs, downloads, model inventory, presets history, audit
events); Phase A uses a slice of it and later phases fill the rest in.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from . import paths

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
  kind TEXT, target TEXT, status TEXT, log TEXT, started_at REAL, ended_at REAL
);
CREATE TABLE IF NOT EXISTS downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT, files TEXT, placement TEXT, status TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY, dir TEXT, role_files TEXT, size_bytes INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS model_files (
  path TEXT PRIMARY KEY, model TEXT, size INTEGER, sha256 TEXT, role TEXT
);
CREATE TABLE IF NOT EXISTS presets_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, note TEXT, before TEXT, after TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
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
