"""Phase C update pipeline: endpoint shape, preflight validation, no-op
detection, rollback validation, and the background job runner.

Nothing here builds llama.cpp or touches the live router — the real pipeline
is proven by the deterministic no-op job in tools/live_smoke.sh.
"""
import subprocess
import threading
import time

from fastapi.testclient import TestClient

from app.core import db, paths, updater
from app.main import app
from app.providers.llamacpp import LlamaCpp


def test_updates_endpoint_shape():
    """Empty temp home: well-formed, no repo, no releases, no job."""
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.get("/api/updates/llamacpp")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"current", "current_commit", "repo_head",
                         "releases", "vulkan_sdk", "job"}
    assert body["current"] is None
    assert body["releases"] == []
    assert body["repo_head"] is None
    assert body["job"] is None


def test_update_rejected_without_repo():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp", json={"target": "HEAD"})
    assert r.status_code == 400
    assert "repo missing" in r.json()["detail"]


def test_rollback_unknown_release():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/rollback",
               json={"release": "llama.cpp-b1", "confirm": True})
    assert r.status_code == 400
    assert "unknown release" in r.json()["detail"]
    # confirm required
    r = c.post("/api/updates/llamacpp/rollback", json={"release": "x"})
    assert r.status_code == 400


def test_job_runner_success_and_exclusivity():
    db.init()
    gate = threading.Event()

    def fn(log):
        log("step one")
        gate.wait(5)
        return {"ok": True}

    jid = updater.start_job("test-tool", "t", fn)
    assert jid is not None
    # one job per kind while one is running
    assert updater.start_job("test-tool", "t2", fn) is None
    gate.set()
    j = _wait(jid)
    assert j["status"] == "success"
    assert "step one" in j["log"]


def test_job_runner_failure_captured():
    def fn(log):
        raise RuntimeError("boom")

    jid = updater.start_job("test-tool-fail", "t", fn)
    j = _wait(jid)
    assert j["status"] == "failed"
    assert "boom" in j["log"]


def test_noop_update_when_target_is_current():
    """target == commit of the deployed release -> update() returns 'noop'
    without building, deploying, or touching the router."""
    repo = paths.LLAMA_REPO
    repo.mkdir(parents=True, exist_ok=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True)

    g("init", "-q")
    (repo / "x.txt").write_text("x")
    g("add", ".")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    head = g("rev-parse", "HEAD").stdout.strip()

    paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.RELEASES_JSON.write_text(
        '{"current": "llama.cpp-b9", "releases": [{"name": "llama.cpp-b9", '
        f'"commit": "{head}", "path": "unused"}}]}}')

    out = LlamaCpp().update("HEAD", restart=False, smoke=False,
                            log=lambda m: None)
    assert out == {"status": "noop", "release": "llama.cpp-b9", "commit": head}


def _wait(jid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = updater.job(jid)
        if j and j["status"] in ("success", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")
