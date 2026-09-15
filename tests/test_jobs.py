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

# Isolation (same contract as tests/test_updates.py): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-jobs-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, systemd, updater  # noqa: E402
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
