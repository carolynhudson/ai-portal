"""Phase C update pipeline: endpoint shape, preflight validation, no-op
detection, rollback validation, and the background job runner.

Nothing here builds llama.cpp or touches the live router — the real pipeline
is proven by the deterministic no-op job in tools/live_smoke.sh.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Isolation: when this file runs alone, test_smoke (which normally sets the
# throwaway AI_PORTAL_HOME first) is never imported — set one here too, or
# paths.* resolves to the REAL ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-test-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

from fastapi.testclient import TestClient

from app.core import db, paths, updater
from app.main import app
from app.providers.llamacpp import LlamaCpp


def test_updates_endpoint_shape(tmp_path, monkeypatch):
    """Empty temp home: well-formed, no repo, no releases, no job."""
    db.init()
    db.execute("DELETE FROM settings WHERE key=?",
               ("llamacpp_remote_state",))
    # the DB is shared across modules in this test home; "no job" has to mean
    # "none here", not "the module that creates jobs happened to run later"
    db.execute("DELETE FROM jobs")
    # same trap, filesystem edition: AI_PORTAL_HOME is shared across modules, and
    # tests/test_stray_builds.py leaves build directories in it. "Empty home" has
    # to mean empty for the thing being asserted, so point the scan at nothing.
    monkeypatch.setattr(paths, "RELEASES", tmp_path / "no-releases")
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.get("/api/updates/llamacpp")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"current", "current_commit", "current_size_bytes",
                         "repo_head", "releases", "release_count",
                         "total_bytes", "vulkan_sdk", "job", "remote",
                         "binary", "source",
                         # unregistered build directories, reported alongside the
                         # list they should have been on
                         "strays", "stray_bytes", "unexpected_builds",
                         "stray_error"}
    assert body["release_count"] == 0 and body["total_bytes"] == 0
    assert body["strays"] == [] and body["stray_bytes"] == 0, \
        "no releases dir must read as no strays, not as an error"
    assert body["stray_error"] is None
    # throwaway home: probe either reports a missing repo or a failed
    # fetch — either way a well-formed dict
    assert isinstance(body["remote"], dict)
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
    if repo.exists():
        shutil.rmtree(repo)
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


# ------------------------------------------------- upstream probe (offline)
# A real local "origin" (bare repo) plus a working clone lets
# remote_state() exercise fetch / tag / behind-counts with no network.

def _upstream_fixture():
    """Bare origin + working clone.  Origin master: A -> B (v0.1.0) -> C.
    The clone sits at A and the release registry says A is deployed.
    Returns (clone, seed, A, B, C) — seed is origin's full worktree, kept
    around so tests can push new upstream commits."""
    base = paths.LLAMA_REPO.parent
    clone, origin, seed = paths.LLAMA_REPO, base / "origin.git", base / "seed"
    for p in (clone, origin, seed):
        if p.exists():
            shutil.rmtree(p)
    db.init()
    db.execute("DELETE FROM settings WHERE key=?",
               ("llamacpp_remote_state",))

    def g(*a, cwd=None):
        return subprocess.run(["git", *a], check=True, capture_output=True,
                              text=True, cwd=cwd)

    g("init", "-q", "-b", "master", str(seed))
    for name in ("a", "b", "c"):
        (seed / "f.txt").write_text(name)
        g("add", ".", cwd=seed)
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
          name, cwd=seed)
    A = g("rev-parse", "HEAD~2", cwd=seed).stdout.strip()
    B = g("rev-parse", "HEAD~1", cwd=seed).stdout.strip()
    C = g("rev-parse", "HEAD", cwd=seed).stdout.strip()
    g("tag", "v0.1.0", B, cwd=seed)
    g("init", "-q", "--bare", str(origin))
    g("remote", "add", "origin", str(origin), cwd=seed)
    g("push", "-q", "origin", "master", cwd=seed)
    g("push", "-q", "origin", "--tags", cwd=seed)
    g("clone", "-q", str(origin), str(clone))
    g("checkout", "-q", A, cwd=clone)
    paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.RELEASES_JSON.write_text(
        '{"current": "test-rel", "releases": [{"name": "test-rel", '
        f'"commit": "{A}", "path": "unused"}}]}}')
    return clone, seed, A, B, C


def test_remote_state_counts_master_and_release():
    _, _, A, B, C = _upstream_fixture()
    st = LlamaCpp().remote_state(force=True)
    assert st["master_head"] == C
    assert st["behind_master"] == 2
    assert st["latest_release"] == "v0.1.0"
    assert st["release_commit"] == B
    assert st["behind_release"] == 1
    assert st["update_available"] is True
    assert st["release_url"] is None  # plain-path origin -> no https URL


def test_remote_state_ttl_cache_and_force():
    clone, seed, A, B, C = _upstream_fixture()
    st1 = LlamaCpp().remote_state(force=True)
    st2 = LlamaCpp().remote_state()  # inside TTL -> same cached snapshot
    assert st2["checked_at"] == st1["checked_at"]
    assert st2["master_head"] == C

    def g(*a, cwd):
        return subprocess.run(["git", *a], check=True, capture_output=True,
                              text=True, cwd=cwd)

    (seed / "d.txt").write_text("d")
    g("add", ".", cwd=seed)
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "d",
      cwd=seed)
    g("push", "-q", "origin", "master", cwd=seed)
    D = g("rev-parse", "HEAD", cwd=seed).stdout.strip()

    st3 = LlamaCpp().remote_state(force=True)  # fresh probe sees D
    assert st3["master_head"] == D
    assert st3["behind_master"] == 3
    assert st3["checked_at"] > st1["checked_at"]
    st4 = LlamaCpp().remote_state()  # now the fresh state is cached
    assert st4["checked_at"] == st3["checked_at"]


def test_resolve_head_uses_remote_default_branch():
    """The deploy button posts target='HEAD'; with a working origin that
    must mean origin/<default-branch>, not local HEAD (regression: the
    symref line's trailing tab used to leak into the ref name)."""
    _, _, A, B, C = _upstream_fixture()
    out = LlamaCpp()._resolve("HEAD", log=lambda m: None)
    assert out == C
    assert out != A
    # explicit shas still resolve verbatim
    assert LlamaCpp()._resolve(A, log=lambda m: None) == A
    assert LlamaCpp()._resolve("v0.1.0", log=lambda m: None) == B


def test_release_url_from_remote():
    clone, _, _, _, _ = _upstream_fixture()
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin",
                    "https://example.com/org/llama.cpp.git"], check=True)
    from app.providers import llamacpp as lc
    assert lc._release_url("v0.1.0") == \
        "https://example.com/org/llama.cpp/releases/tag/v0.1.0"


def test_remote_state_offline_returns_stale_cache():
    clone, _, _, _, C = _upstream_fixture()
    st = LlamaCpp().remote_state(force=True)
    assert "stale" not in st
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin",
                    "/nonexistent/origin.git"], check=True)
    st2 = LlamaCpp().remote_state(force=True)  # fetch must fail
    assert st2.get("stale") is True
    assert st2.get("error")
    assert st2["master_head"] == C  # last good state preserved


def test_check_endpoint_forces_probe():
    _, _, A, B, C = _upstream_fixture()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/check")
    assert r.status_code == 200
    body = r.json()
    assert body["master_head"] == C
    assert body["behind_release"] == 1
    assert body["update_available"] is True


# ------------------------------------------------------ deleting stored builds
# The dashboard lists every build in one dropdown and offers to delete the
# selected one, so the server is the only thing standing between a stray click
# and pulling the binaries out from under a running router. Each refusal here is
# a test the UI's lockout is not trusted to be the only defence.

def _fake_builds(current="llama.cpp-b2"):
    """A registry with two real build directories and a ~/ai/bin farm pointing
    at the current one. Returns {name: path}."""
    db.init()
    paths.RELEASES.mkdir(parents=True, exist_ok=True)
    paths.AI_BIN.mkdir(parents=True, exist_ok=True)
    entries, dirs = [], {}
    for name in ("llama.cpp-b1", "llama.cpp-b2"):
        d = paths.RELEASES / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "llama-server").write_bytes(b"x" * 2048)
        (d / "llama-cli").write_bytes(b"y" * 1024)
        dirs[name] = d
        entries.append({"name": name, "commit": ("a" if name.endswith("1") else "b") * 40,
                        "version": name.rsplit("b", 1)[-1], "path": str(d),
                        "built_at": "2026-09-01T00:00:00+00:00"})
    paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.RELEASES_JSON.write_text(json.dumps({"current": current,
                                               "releases": entries}))
    _repoint_farm(dirs[current])
    return dirs


def _repoint_farm(rel_dir: Path):
    for exe in sorted(rel_dir.iterdir()):
        link = paths.AI_BIN / exe.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(exe)


def test_update_status_marks_and_sizes_builds():
    _fake_builds()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    body = c.get("/api/updates/llamacpp").json()
    assert body["release_count"] == 2
    assert body["current"] == "llama.cpp-b2"
    active = [e for e in body["releases"] if e["active"]]
    assert [e["name"] for e in active] == ["llama.cpp-b2"]
    assert active[0]["size_bytes"] >= 3072
    assert body["total_bytes"] >= sum(e["size_bytes"] for e in body["releases"])


def test_delete_requires_confirmation_and_a_known_name():
    _fake_builds()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/delete", json={"release": "llama.cpp-b1"})
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    r = c.post("/api/updates/llamacpp/delete",
               json={"release": "llama.cpp-b99", "confirm": True})
    assert r.status_code == 400 and "unknown release" in r.json()["detail"]


def test_delete_refuses_the_current_build():
    dirs = _fake_builds()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/delete",
               json={"release": "llama.cpp-b2", "confirm": True})
    assert r.status_code == 400
    assert "current build" in r.json()["detail"]
    assert (dirs["llama.cpp-b2"] / "llama-server").exists()


def test_delete_removes_the_build_and_remembers_it_did():
    dirs = _fake_builds()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    before = len(db.query("SELECT * FROM events"))
    r = c.post("/api/updates/llamacpp/delete",
               json={"release": "llama.cpp-b1", "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success" and body["remaining"] == 1
    assert body["freed_bytes"] >= 3072
    assert not dirs["llama.cpp-b1"].exists()
    assert (dirs["llama.cpp-b2"] / "llama-server").exists()

    reg = json.loads(paths.RELEASES_JSON.read_text())
    assert [e["name"] for e in reg["releases"]] == ["llama.cpp-b2"]
    assert reg["current"] == "llama.cpp-b2"        # untouched

    # the registry is copied out first, and the audit log says what was freed
    note = Path(body["note_dir"])
    assert json.loads((note / "releases.json").read_text())["current"] == "llama.cpp-b2"
    assert "llama.cpp-b1" in (note / "note.txt").read_text()
    assert len(db.query("SELECT * FROM events")) > before

    # and the page now reports one build, none of them deletable-but-current
    body = c.get("/api/updates/llamacpp").json()
    assert body["release_count"] == 1
    assert [e for e in body["releases"] if e["active"]] == body["releases"]


def test_delete_refuses_a_registry_pointing_anywhere_else():
    """A hand-edited registry must not turn the delete button into `rmtree ~`."""
    dirs = _fake_builds()
    reg = json.loads(paths.RELEASES_JSON.read_text())
    victim = next(e for e in reg["releases"] if e["name"] == "llama.cpp-b1")
    victim["path"] = str(paths.AI_BIN)
    paths.RELEASES_JSON.write_text(json.dumps(reg))

    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/delete",
               json={"release": "llama.cpp-b1", "confirm": True})
    assert r.status_code == 400
    assert "outside" in r.json()["detail"]
    assert paths.AI_BIN.is_dir()


def test_delete_refuses_while_the_bin_farm_points_at_it():
    """Registry says it is not current; the farm says otherwise. The farm wins."""
    dirs = _fake_builds()
    _repoint_farm(dirs["llama.cpp-b1"])

    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/updates/llamacpp/delete",
               json={"release": "llama.cpp-b1", "confirm": True})
    assert r.status_code == 400
    assert "~/ai/bin still points" in r.json()["detail"]
    assert (dirs["llama.cpp-b1"] / "llama-server").exists()
