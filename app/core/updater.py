"""Background update-job runner (Phase C; reused for the ComfyUI updater in E).

Update pipelines are long (a llama.cpp build takes 10–20 min) and can't be
synchronous HTTP handlers. Each job runs on a daemon thread and is tracked in
the ``jobs`` table (portal.db); its log goes to ``~/ai/logs/update-<kind>-<id>.log``
and stays tail-able via :func:`job` for the UI's live progress pane.

**One job at a time, machine-wide.** Not one per kind: a llama.cpp build and a
ComfyUI venv sync at the same moment are two long dependency moves against the
same GPU stack, and either can restart a service the other is using. The rule is
therefore global — while an exclusive job runs, no other exclusive job starts and
no service may be started, stopped or restarted (see :func:`blocker`, which the
API turns into a 409 that says what is busy and why).

The job row is the source of truth, not this process: a job keeps running when
the browser navigates to another page, and its log stays readable. What a portal
restart does kill (the thread), and a row older than ``_STALE_AFTER`` is
reclaimed as failed so the machine is not locked forever.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import threading
import time
from pathlib import Path
import traceback
from collections import deque

from . import db, paths

#: What the UI calls each kind of job. A bar that says "llamacpp" when a build
#: is running is the machine's vocabulary, not the operator's.
KIND_LABEL = {
    "llamacpp": "llama.cpp build",
    "comfyui": "ComfyUI update",
    "comfyui-download": "ComfyUI model download",
    "model-add": "model add",
    "huggingface": "HF CLI update",
}

#: Jobs that take the machine's side. A build, a venv sync or a CLI upgrade
#: restarts services, replaces binaries a running service is using, and saturates
#: disk and network — so one of these wants the machine to itself, and no service
#: may be started, stopped or restarted while it runs. Downloads are not in this
#: set: they move files, they do not disturb what is running (but they will not
#: start on top of a disruptive job either).
# `presets-apply` is here because it restarts ai-llama-router as its final step. Without this
# entry an update job could swap the llama.cpp release out from under a presets apply that is
# mid-restart, or an apply could restart a router whose binary a build is currently replacing.
# The write itself is cheap; the restart is what makes it machine-wide.
DISRUPTIVE = {"llamacpp", "comfyui", "huggingface", "hf", "python",
              "presets-apply", "gpuupdate"}

_TAIL = 300                # log lines kept in the DB for the API
_STALE_AFTER = 6 * 3600    # backstop for rows whose writer cannot be identified


def _boot_id() -> str | None:
    """A token for this boot of this machine, or None if the OS won't say.

    A job row carries it so the next boot can tell, immediately, that a row still
    saying 'running' belongs to a world that no longer exists — rather than
    believing it for six hours and locking the machine behind it.
    """
    try:
        return hashlib.sha1(Path("/proc/sys/kernel/random/boot_id")
                            .read_text().encode()).hexdigest()[:12]
    except Exception:  # noqa: BLE001 — non-Linux, or masked: fall back to age
        return None


BOOT = _boot_id()

_lock = threading.Lock()
_running: dict[str, int] = {}   # kind -> job id (this process)


def _touch(job_id: int, status: str, log: str,
           ended: float | None = None) -> None:
    db.execute("UPDATE jobs SET status=?, log=?, ended_at=? WHERE id=?",
               (status, log, ended, job_id))
    _live[job_id] = log          # see _current_log


def _human(result) -> str:
    """A job's return value, in the few words the status bar has room for."""
    if not isinstance(result, dict):
        return str(result)[:120]
    bits = []
    status = str(result.get("status") or "")
    bits.append({"noop": "nothing to do", "success": "done"}.get(status, status))
    changed = result.get("changed")
    if isinstance(changed, dict):
        bits.append(f"{len(changed)} package(s) changed" if changed
                    else "no package changes")
    if result.get("commit"):
        bits.append(str(result["commit"])[:12])
    if "restarted" in result:
        bits.append("service restarted" if result["restarted"]
                    else "service left running")
    return ", ".join(b for b in bits if b) or str(result)[:120]


def step_of(job: dict) -> str:
    """This job's current step: its newest log line, timestamp stripped."""
    lines = [ln for ln in (job.get("log") or "").strip().splitlines() if ln]
    return re.sub(r"^\[[\d:]+\]\s*", "", lines[-1])[:140] if lines else ""


def _current_log(job_id: int) -> str:
    """The freshest log this process holds for a job.

    A row read from the DB at the instant a job starts can carry an empty log
    while the thread is already writing: the refusal a second request gets must
    not lose the step for that reason. Rows written by *this* process are
    authoritative; anything else (another worker, a restart) falls back to the
    row's own log.
    """
    return _live.get(job_id, "")


# job_id -> latest log, for the process that is running the job. Bounded by the
# job history one keeps in memory being small; entries are dropped when a job
# ends and its result is in the DB.
_live: dict[int, str] = {}


def _writer_gone(row) -> bool:
    """Is the process that wrote this row definitely gone?

    Two structural proofs, both immediate, because a reboot that interrupts a
    build must not cost six hours of a locked UI (that is the bug this closes:
    the bar reported a 10-hour-old build as running on a machine that had been
    rebooted for an hour):

    * the row was written in an **earlier boot** — that process cannot exist;
    * same boot, but its **pid is gone** (the portal was restarted, updated, or
      killed mid-job). A pid we cannot signal-for-existence at all is assumed
      alive, and a row with neither pid nor boot (written before this existed)
      falls through to the age backstop.
    """
    boot = row["boot"] if "boot" in row.keys() else None
    if boot and BOOT and boot != BOOT:
        return True
    pid = row["pid"] if "pid" in row.keys() else None
    if pid and pid != os.getpid():
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except Exception:  # noqa: BLE001 — not ours to signal ⇒ assume alive
            return False
    return False


def _reclaim_stale() -> None:
    """Reclaim 'running' rows whose writer is gone, and as a backstop any row
    older than ``_STALE_AFTER``. A job that is believed alive locks the whole
    machine, so a dead row left standing is not a cosmetic problem."""
    rows = db.query("SELECT id, started_at, pid, boot FROM jobs "
                    "WHERE status='running'")
    cutoff = time.time() - _STALE_AFTER
    for r in rows:
        if not _writer_gone(r) and (r["started_at"] or 0) >= cutoff:
            continue
        note = ("(stale: written by a previous boot)"
                if (r["boot"] if "boot" in r.keys() else None) not in (None, BOOT)
                else "(stale: portal restarted mid-job)")
        # COALESCE, not bare `log ||`: a job that died before its first log line
        # has NULL there, and NULL || text is NULL — the note, and with it the
        # only clue to what happened, would vanish.
        db.execute("UPDATE jobs SET status='failed', ended_at=?, "
                   "log=COALESCE(log,'') || char(10) || ? WHERE id=?",
                   (time.time(), note, r["id"]))
        _running.pop(next((k for k, v in _running.items() if v == r["id"]),
                          ""), None)


def running() -> list[dict]:
    """Every job currently in flight (newest first), as the API reports it."""
    with _lock:
        _reclaim_stale()
        rows = db.query("SELECT id FROM jobs WHERE status='running' "
                        "ORDER BY id DESC")
    return [j for j in (job(r["id"]) for r in rows) if j]


def blocker(kinds: set[str] | None = None) -> dict | None:
    """The job that owns the machine right now, for a refusal message.

    Everything that can disrupt a running service asks this first. The answer is
    a job dict, so the caller can say *which* update is busy and how far along —
    "an update is already running" is not something an operator can act on.

    ``kinds`` narrows it (e.g. only :data:`DISRUPTIVE` jobs hold a service
    restart; a model download in flight does not).
    """
    for j in running():
        if kinds is None or j["kind"] in kinds:
            return j
    return None


def brief(job: dict) -> dict:
    """A job as the status bar consumes it: label, elapsed, current step."""
    started = job.get("started_at") or time.time()
    ended = job.get("ended_at")
    tail_lines = [ln for ln in (job.get("log") or "").strip().splitlines() if ln]
    step = step_of(job)
    return {
        "id": job["id"],
        "kind": job["kind"],
        "label": KIND_LABEL.get(job["kind"], job["kind"]),
        "target": job.get("target"),
        "status": job["status"],
        "started_at": started,
        "ended_at": ended,
        "elapsed_s": round((ended or time.time()) - started, 1),
        "step": step,
        "log_tail": "\n".join(tail_lines[-3:]),
    }


def payload(now: float | None = None, last_window: float = 30 * 60) -> dict:
    """The machine's busy/idle state for the global status bar.

    ``last`` is the most recent *finished* job inside ``last_window`` seconds: an
    update that completed while you were on another page should still tell you it
    finished, and whether it worked, instead of the bar simply vanishing.
    """
    jobs = running()
    last = None
    row = db.query("SELECT id FROM jobs WHERE status<>'running' "
                   "ORDER BY id DESC LIMIT 1")
    if row:
        candidate = job(row[0]["id"])
        if candidate and (now or time.time()) - (candidate.get("ended_at") or 0) \
                < last_window:
            last = brief(candidate)
    return {"running": [brief(j) for j in jobs],
            "blocker": brief(jobs[0]) if jobs else None,
            "last": last}


def busy_reason(kinds: set[str] | None = None) -> str:
    """Why the machine will not accept this right now, in one honest line."""
    j = blocker(kinds)
    return describe(j) if j else "another job is running"


def describe(job: dict) -> str:
    """One human line: 'ComfyUI update of master (job 21) — syncing venv'.

    The step is *this* job's newest line, falling back to the copy this process
    is holding for it: a row read at the moment a job starts can still be empty
    while its thread is already writing. (It was once "the freshest line in the
    whole table", which produced "busy — ComfyUI update master (job 22) —
    success: {...}": a refusal blamed on a job that had already finished, because
    the row reached here before the running job had logged anything.)
    """
    started = datetime.datetime.fromtimestamp(job["started_at"])
    label = KIND_LABEL.get(job["kind"], job["kind"])
    step = step_of(job)[:110] or _current_log(job["id"])[:110]
    return f"{label} {job.get('target') or ''}".strip() + \
        f" (job {job['id']}, since {started:%H:%M:%S})" + (f" — {step}" if step else "")


def start_job(kind: str, target: str, fn, exclusive: bool | None = None) -> int | None:
    """Run ``fn(log)`` on a background thread as a job.

    ``fn`` receives a ``log(str)`` callback; its return value is recorded as the
    result and any exception it raises fails the job. Returns the job id, or
    ``None`` if the machine is busy.

    ``exclusive`` defaults to "is this kind in :data:`DISRUPTIVE`". An exclusive
    job starts only on an otherwise idle machine; a non-exclusive one (a download)
    starts unless a disruptive job is running or another of its own kind is.
    """
    if exclusive is None:
        exclusive = kind in DISRUPTIVE
    with _lock:
        _reclaim_stale()
        # the jobs table is the source of truth: a job outlives the request that
        # started it, and a second portal worker (or a reload) must still see it
        busy = db.query("SELECT id, kind, started_at FROM jobs "
                        "WHERE status='running' ORDER BY id DESC")
        if busy:
            if exclusive:
                return None
            kinds = [r["kind"] for r in busy]
            if kind in kinds or any(k in DISRUPTIVE for k in kinds):
                return None
        # pid + boot: the record of who can still be writing this row. Without
        # them a row can only be judged dead by age, and every portal restart that
        # interrupts a job would lock the machine for hours on a job that is not
        # running.
        db.execute("INSERT INTO jobs (kind, target, status, started_at, pid, boot) "
                   "VALUES (?,?,?,?,?,?)",
                   (kind, target, "running", time.time(), os.getpid(), BOOT))
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
        # The bar shows this line, so it is written for a person reading it from
        # across the room. The full result still goes to the job's file log for
        # archaeology — a finished update that ended in
        # "success: {'status': 'noop', 'commit': '36da3ff…'}" told you less than
        # the one word "nothing to do".
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] success (raw): {result}\n")
        log("success" + (": " + _human(result) if result else ""))
        status = "success"
    except Exception:  # noqa: BLE001 — the job's fate, not the portal's
        log(traceback.format_exc())
    finally:
        with _lock:
            _running.pop(kind, None)
        _touch(job_id, status, "\n".join(lines), time.time())
        _live.pop(job_id, None)   # the row in the DB is the truth from here


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
