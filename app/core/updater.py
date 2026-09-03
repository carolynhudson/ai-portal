"""Background update-job runner (Phase C; reused for the ComfyUI updater in E).

Update pipelines are long (a llama.cpp build takes 10–20 min) and can't be
synchronous HTTP handlers. Each job runs on a daemon thread and is tracked in
the ``jobs`` table (portal.db); its log goes to ``~/ai/logs/update-<kind>-<id>.log``
and stays tail-able via :func:`job` for the UI's live progress pane.

Only one job per ``kind`` may run at a time, so a second click while a build
is in flight is rejected instead of double-building.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque

from . import db, paths

_TAIL = 300                # log lines kept in the DB for the API
_STALE_AFTER = 6 * 3600    # a 'running' row older than this = a dead portal

_lock = threading.Lock()
_running: dict[str, int] = {}   # kind -> job id (this process)


def _touch(job_id: int, status: str, log: str,
           ended: float | None = None) -> None:
    db.execute("UPDATE jobs SET status=?, log=?, ended_at=? WHERE id=?",
               (status, log, ended, job_id))


def start_job(kind: str, target: str, fn) -> int | None:
    """Run ``fn(log)`` on a background thread as an update job.

    ``fn`` receives a ``log(str)`` callback; its return value is recorded as the
    result and any exception it raises fails the job. Returns the job id, or
    ``None`` if a job for this kind is already running.
    """
    with _lock:
        if kind in _running:
            return None
        row = db.query("SELECT id, status, started_at FROM jobs WHERE kind=? "
                       "ORDER BY id DESC LIMIT 1", (kind,))
        if row and row[0]["status"] == "running":
            if time.time() - row[0]["started_at"] < _STALE_AFTER:
                return None
            # a portal killed mid-job leaves a 'running' row behind — clear it
            db.execute("UPDATE jobs SET status='failed', ended_at=?, "
                       "log=log || char(10) || ? WHERE id=?",
                       (time.time(), "(stale: portal restarted mid-job)",
                        row[0]["id"]))
        db.execute("INSERT INTO jobs (kind, target, status, started_at) "
                   "VALUES (?,?,?,?)", (kind, target, "running", time.time()))
        job_id = db.query("SELECT last_insert_rowid() AS id")[0]["id"]
        _running[kind] = job_id
    threading.Thread(target=_run, args=(kind, job_id, fn), daemon=True).start()
    return job_id


def _run(kind: str, job_id: int, fn) -> None:
    lines: deque[str] = deque(maxlen=_TAIL)
    log_path = paths.LOGS / f"update-{kind}-{job_id}.log"
    paths.LOGS.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        lines.append(line)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _touch(job_id, "running", "\n".join(lines))

    status = "failed"
    try:
        result = fn(log)
        log(f"success: {result}" if result else "success")
        status = "success"
    except Exception:  # noqa: BLE001 — the job's fate, not the portal's
        log(traceback.format_exc())
    finally:
        with _lock:
            _running.pop(kind, None)
        _touch(job_id, status, "\n".join(lines), time.time())


def job(job_id: int) -> dict | None:
    row = db.query("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        return None
    r = row[0]
    return {"id": r["id"], "kind": r["kind"], "target": r["target"],
            "status": r["status"], "log": r["log"] or "",
            "started_at": r["started_at"], "ended_at": r["ended_at"]}


def latest_job(kind: str) -> dict | None:
    row = db.query("SELECT id FROM jobs WHERE kind=? ORDER BY id DESC LIMIT 1",
                   (kind,))
    return job(row[0]["id"]) if row else None
