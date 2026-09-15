"""The job runner: what a long action looks like from outside the page that
started it.

The properties this suite exists to keep:

* **A job outlives its request.** It is a row plus a thread, not a coroutine
  attached to a browser. Navigating to another page, closing the tab, or a second
  browser looking at `/api/jobs/active` must all be observations, never causes.
* **One disruptive thing at a time.** A build, a venv sync and a CLI upgrade all
  replace binaries or packages that a running service is using, and all can
  restart it. While one runs, no other starts and no service is stopped or
  restarted — refused server-side, because a disabled button is a courtesy and
  not a guarantee.
* **A refusal says what is busy and how far along.** "already running" is not
  actionable; "llama.cpp build of HEAD, since 10:14 — cmake building" is.

Nothing here builds anything or touches systemd: jobs are trivial functions, and
`systemd.service_action` is stubbed.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

# Isolation (same contract as tests/test_updates.py): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-jobs-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, paths, systemd, updater  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_jobs():
    """Jobs live in the shared portal.db of this test home, so leave no rows
    behind: another module asserting "no job yet" must not depend on running
    before this one."""
    db.init()
    db.execute("DELETE FROM jobs")
    updater._running.clear()
    yield
    updater._running.clear()
    db.execute("DELETE FROM jobs")


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


class Held:
    """A job parked until the test releases it — so "while it runs" is testable
    without sleeps."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.log_lines: list[str] = []

    def fn(self, log):
        log("step one")
        self.started.set()   # set after the first line: "running" must be readable
        assert self.release.wait(timeout=10), "test never released the job"
        log("step two")
        return "done"

    def finish(self):
        self.release.set()
        self.started.wait(timeout=10)


def wait_for_job(kind: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = updater.blocker({kind})
        if j:
            return j
        time.sleep(0.02)
    raise AssertionError(f"no running {kind} job")


def settle(timeout: float = 5.0):
    """Wait for every job row to leave 'running'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not updater.running():
            return
        time.sleep(0.02)
    raise AssertionError("jobs still running")


# ------------------------------------------------- a job is not owned by a page

def test_a_job_runs_without_any_page_looking():
    """The property the complaint was about: start it, look away, it still works."""
    h = Held()
    job_id = updater.start_job("llamacpp", "HEAD", h.fn)
    assert job_id
    try:
        h.started.wait(timeout=5)
        # no request in flight, no page open: the row is still there and moving
        row = updater.job(job_id)
        assert row["status"] == "running"
        assert "step one" in row["log"]
    finally:
        h.finish()
    settle()
    assert updater.job(job_id)["status"] == "success"
    assert "step two" in updater.job(job_id)["log"]


def test_active_reports_the_running_job_in_human_terms(client):
    h = Held()
    job_id = updater.start_job("comfyui", "master", h.fn)
    try:
        h.started.wait(timeout=5)
        body = client.get("/api/jobs/active").json()
        assert len(body["running"]) == 1
        run = body["running"][0]
        assert run["id"] == job_id
        assert run["label"] == "ComfyUI update", "the bar must not speak 'comfyui'"
        assert run["status"] == "running"
        assert run["elapsed_s"] >= 0
        assert run["step"] == "step one", "the bar shows the current step, not a spinner"
        assert body["blocker"]["id"] == job_id
    finally:
        h.finish()
    settle()


def test_active_holds_on_to_the_last_finished_job(client):
    h = Held()
    job_id = updater.start_job("comfyui", "master", h.fn)
    h.finish()
    settle()
    body = client.get("/api/jobs/active").json()
    assert body["running"] == []
    assert body["last"]["id"] == job_id
    assert body["last"]["status"] == "success"
    assert body["blocker"] is None


def test_idle_machine_reports_nothing_to_show(client):
    body = client.get("/api/jobs/active").json()
    assert body["running"] == [] and body["blocker"] is None
    assert body["last"] is None


def test_the_per_job_endpoint_still_works(client):
    h = Held()
    job_id = updater.start_job("comfyui", "master", h.fn)
    try:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200 and r.json()["kind"] == "comfyui"
        assert client.get("/api/jobs/999999").status_code == 404
    finally:
        h.finish()
    settle()


# ------------------------------------------- one disruptive thing at a time

def test_a_build_blocks_a_comfyui_update():
    h = Held()
    updater.start_job("llamacpp", "HEAD", h.fn)
    try:
        wait_for_job("llamacpp")
        assert updater.start_job("comfyui", "master", h.fn) is None, \
            "two updates at once can swap the same venv and services"
    finally:
        h.finish()
    settle()


def test_a_comfyui_update_blocks_a_build():
    h = Held()
    updater.start_job("comfyui", "master", h.fn)
    try:
        wait_for_job("comfyui")
        assert updater.start_job("llamacpp", "HEAD", h.fn) is None
    finally:
        h.finish()
    settle()


def test_a_running_update_is_refused_over_api_naming_the_job(client):
    h = Held()
    updater.start_job("comfyui", "master", h.fn)
    try:
        wait_for_job("comfyui")
        r = client.post("/api/updates/llamacpp", json={"target": "HEAD"})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail.startswith("busy — ")
        assert "ComfyUI update" in detail, detail
    finally:
        h.finish()
    settle()


def test_a_running_update_blocks_restarting_a_service(client, monkeypatch):
    """Stopping the thing an update is mid-way through swapping is how you get
    half-applied state, so the request is refused, not warned about."""
    called: list = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda key, action: called.append((key, action)) or {"ok": True})
    h = Held()
    updater.start_job("llamacpp", "HEAD", h.fn)
    try:
        wait_for_job("llamacpp")
        r = client.post("/api/services/comfyui/restart", json={"confirm": True})
        assert r.status_code == 409 and "llama.cpp build" in r.json()["detail"]
        r = client.post("/api/services/comfyui/stop", json={"confirm": True})
        assert r.status_code == 409
        assert called == [], "refused means the unit was not touched"
    finally:
        h.finish()
    settle()


def test_a_service_action_is_allowed_again_once_the_job_is_done(client, monkeypatch):
    called: list = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda key, action: called.append((key, action)) or {"ok": True})
    h = Held()
    updater.start_job("llamacpp", "HEAD", h.fn)
    h.finish()
    settle()
    r = client.post("/api/services/comfyui/restart", json={"confirm": True})
    assert r.status_code == 200
    assert called == [("comfyui", "restart")]


def test_rollback_is_refused_while_a_job_runs(client):
    h = Held()
    updater.start_job("comfyui", "master", h.fn)
    try:
        wait_for_job("comfyui")
        r = client.post("/api/updates/llamacpp/rollback",
                        json={"release": "llama.cpp-b1", "confirm": True})
        assert r.status_code == 409
    finally:
        h.finish()
    settle()


# ---------------------------------------------------------------- downloads

def test_a_download_may_share_the_machine_with_itself_not_with_an_update():
    """Downloads move files; they do not disturb what runs. But two of one kind
    would race the same index, and no download starts on top of an update."""
    h = Held()
    updater.start_job("model-add", "one.gguf", h.fn)
    try:
        wait_for_job("model-add")
        assert updater.start_job("model-add", "two.gguf", h.fn) is None
        assert updater.start_job("comfyui", "master", h.fn) is None, \
            "a venv sync under a running download is the wrong kind of company"
    finally:
        h.finish()
    settle()


def test_a_download_does_not_hold_a_service_restart(client, monkeypatch):
    called: list = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda key, action: called.append((key, action)) or {"ok": True})
    h = Held()
    updater.start_job("comfyui-download", "https://example/x.gguf", h.fn)
    try:
        wait_for_job("comfyui-download")
        r = client.post("/api/services/comfyui/restart", json={"confirm": True})
        assert r.status_code == 200
        assert called == [("comfyui", "restart")]
    finally:
        h.finish()
    settle()


# --------------------------------------------------- staleness: is the writer alive?

def _row(**kw) -> int:
    kw.setdefault("kind", "llamacpp")
    kw.setdefault("status", "running")
    kw.setdefault("started_at", time.time() - 120)   # well inside every timeout
    cols = ", ".join(kw)
    q = ",".join("?" * len(kw))
    db.execute(f"INSERT INTO jobs ({cols}) VALUES ({q})", tuple(kw.values()))
    return db.query("SELECT last_insert_rowid() AS id")[0]["id"]


def test_a_row_from_an_earlier_boot_is_not_a_running_job():
    """The machine rebooted, so whatever that thread was doing is over.

    This is the bug: a reboot during a build left the row saying "running", and
    for six hours the bar reported a job that could not exist and locked every
    disruptive control behind it — on a machine that had been up for an hour.
    Believing a row because it is young is not the same as knowing it is alive.
    """
    jid = _row(boot="a-boot-that-is-not-this-one", pid=999999)
    assert updater.running() == [], "a previous boot's job is not running"
    row = updater.job(jid)
    assert row["status"] == "failed"
    assert "previous boot" in row["log"]


def test_a_previous_boot_wins_over_a_pid_that_happens_to_exist(monkeypatch):
    """A pid is a recycled number. After a reboot the old writer is gone even if
    something alive answers to its pid now — so the boot token, not the pid, is
    the decisive fact."""
    monkeypatch.setattr(updater, "BOOT", "this-boot")
    jid = _row(boot="the-boot-before", pid=os.getpid())   # this pid: definitely alive
    assert updater.running() == []
    assert "previous boot" in updater.job(jid)["log"]


def test_boot_detection_actually_works_here():
    """Not a tautology: `_boot_id` swallows every error to stay probe-safe, which
    is exactly how it once returned None forever from a missing `import Path` —
    and every boot-based test still passed through the pid branch. If the OS
    offers a boot id, the module must have read it."""
    if Path("/proc/sys/kernel/random/boot_id").is_file():
        assert updater.BOOT and len(updater.BOOT) == 12
        assert updater.BOOT == updater._boot_id(), "recomputed per call?"


def test_a_row_whose_process_is_gone_is_not_a_running_job():
    """Same boot, but the portal was restarted/updated/killed mid-job."""
    jid = _row(boot=updater.BOOT, pid=999999)
    assert updater.running() == []
    assert updater.job(jid)["status"] == "failed"


def test_a_job_this_process_is_writing_is_still_running():
    jid = _row(boot=updater.BOOT, pid=os.getpid())
    assert [j["id"] for j in updater.running()] == [jid]
    db.execute("DELETE FROM jobs WHERE id=?", (jid,))


def test_the_bar_unlocks_the_page_after_a_reboot(client, monkeypatch):
    """What the operator sees: the page must not be locked by an impossible job.

    Asserted through the endpoint the bar polls, because that response is what
    sets `data-jobbusy` and disables every update control on every page.
    """
    monkeypatch.setattr(systemd, "service_action",
                        lambda key, action: {"ok": True})
    _row(boot="earlier-boot", pid=999999)
    body = client.get("/api/jobs/active").json()
    assert body["running"] == [] and body["blocker"] is None
    r = client.post("/api/services/comfyui/restart", json={"confirm": True})
    assert r.status_code == 200, "a dead job refused a live service action"


def test_a_new_job_stamps_who_wrote_it(monkeypatch):
    """The liveness rule is only as good as the stamp, so pin it."""
    monkeypatch.setattr(updater, "BOOT", "test-boot-token")
    jid = updater.start_job("model-add", "x.gguf", lambda log: "ok")
    settle()
    row = db.query("SELECT pid, boot FROM jobs WHERE id=?", (jid,))[0]
    assert row["pid"] == os.getpid() and row["boot"] == "test-boot-token"


# ------------------------------------------------------------------- staleness

def test_a_row_left_by_a_dead_portal_is_reclaimed_not_fatal():
    """A portal killed mid-build must not lock the machine for ever."""
    db.execute("INSERT INTO jobs (kind, target, status, started_at) "
               "VALUES (?,?,?,?)",
               ("llamacpp", "HEAD", "running", time.time() - 7 * 3600))
    assert updater.start_job("llamacpp", "HEAD", lambda log: "ok") is not None
    settle()
    rows = db.query("SELECT status, log FROM jobs ORDER BY id DESC")
    stale = [r for r in rows if "stale" in (r["log"] or "")]
    assert stale and stale[0]["status"] == "failed"


def test_a_fresh_running_row_is_not_stale():
    db.execute("INSERT INTO jobs (kind, target, status, started_at) "
               "VALUES (?,?,?,?)",
               ("llamacpp", "HEAD", "running", time.time() - 60))
    assert updater.start_job("llamacpp", "HEAD", lambda log: "ok") is None


# ------------------------------------------------------------------- shape

def test_payload_shape_is_stable_for_the_bar():
    h = Held()
    updater.start_job("comfyui", "master", h.fn)
    try:
        wait_for_job("comfyui")
        p = updater.payload()
        for key in ("running", "blocker", "last"):
            assert key in p
        j = p["running"][0]
        assert set(j) >= {"id", "kind", "label", "target", "status",
                          "started_at", "elapsed_s", "step", "log_tail"}
    finally:
        h.finish()
    settle()


# ------------------------------------------------------------------- refusal text

def test_a_refusal_shows_the_busy_jobs_own_step_not_someones_last_line():
    """The step in a refusal belongs to the job named in it.

    It used to be "the newest line in the whole table": with a second request
    arriving just as another job finished, the row was still empty and the line
    came back from wherever else a log line lived — 'busy — ComfyUI update
    master (job 22) — success: {…}', blaming a refusal on a finished job. Live,
    on the machine, 30 seconds after the updater was written.
    """
    h = Held()
    job_id = updater.start_job("llamacpp", "HEAD", h.fn)
    try:
        wait_for_job("llamacpp")
        # a *finished* job with a newer id, whose log must never leak into a
        # refusal about the running one
        done = updater.start_job("comfyui", "master", lambda log: "unrelated work completed ok")
        for _ in range(200):
            if (updater.job(done) or {}).get("status") == "success":
                break
            time.sleep(0.02)
        reason = updater.busy_reason(updater.DISRUPTIVE)
        assert f"job {job_id}" in reason, reason
        assert "step one" in reason, reason
        assert "unrelated work completed" not in reason, reason
    finally:
        h.finish()
    settle()


def test_a_refusal_survives_a_row_that_has_not_logged_yet(monkeypatch):
    """The row can be read a beat before the thread's first write; the live copy
    this process holds covers that window."""
    row = {"id": 991, "kind": "comfyui", "target": "master", "status": "running",
           "log": "", "started_at": time.time() - 30}
    updater._live[991] = "[10:00:01] syncing venv: install -q -r requirements.txt"
    try:
        assert "syncing venv" in updater.describe(row)
    finally:
        updater._live.pop(991, None)


def test_a_finished_job_says_it_in_words_not_in_python():
    """The step line is read from across the room; a repr is not an answer.

    Live, the bar said: "ComfyUI update master finished after 1s — success:
    {'status': 'noop', 'commit': '36da3ff763687eab86a35e1019995dd1fb369b0d', …}",
    which is both unreadable and less informative than "nothing to do". The full
    result still belongs to the record, so it goes to the job's file log.
    """
    jid = updater.start_job(
        "comfyui", "master",
        lambda log: {"status": "noop", "commit": "3" * 40,
                     "python": "/home/x/venv/bin/python"})
    settle()
    row = updater.job(jid)
    assert row["status"] == "success"
    assert "nothing to do" in row["log"], row["log"]
    assert "'status'" not in row["log"], "a Python dict was shown in the bar"
    assert "'status': 'noop'" in (paths.LOGS / f"update-comfyui-{jid}.log").read_text(), \
        "the full result vanished from the record"


def test_a_failing_job_fails_loudly_in_its_own_row():
    def boom(log):
        log("about to fail")
        raise RuntimeError("disk gone")

    job_id = updater.start_job("llamacpp", "HEAD", boom)
    settle()
    row = updater.job(job_id)
    assert row["status"] == "failed"
    assert "disk gone" in row["log"]
    # and the machine is free again
    assert updater.start_job("comfyui", "master", lambda log: "ok") is not None
    settle()
