"""Unregistered build directories: finding them, and deleting them safely.

An update that dies between staging and recording leaves a complete ~400 MB
build directory that no list shows — `delete build` refuses it, because that
path's first guard is "is it in the registry", which is the one thing that
cannot be true here. So this is a second, deliberately narrower path, and most
of what it has to prove is that it refuses.

Runs against a throwaway AI_PORTAL_HOME: nothing here touches ~/ai.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-stray-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, health, paths  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import llamacpp  # noqa: E402
from app.providers.llamacpp import LlamaCpp  # noqa: E402

MB = 1 << 20


def _build_dir(name: str, files: int = 3, size: int = 5 * MB,
               version: str | None = "version: 1.2.3 (build 9999, commit abc1234)") -> Path:
    """A build directory that looks like a staged release, at no disk cost."""
    d = paths.RELEASES / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"lib{i}.so").write_bytes(b"\0" * size)
    if version is not None:
        exe = d / "llama-server"
        exe.write_text(f"#!/bin/sh\necho '{version}'\n")
        exe.chmod(0o755)
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A ~/ai with one registered current build, farmed into ~/ai/bin."""
    # start from a clean slate every time: these tests create and destroy the
    # same handful of paths, and a link left over from the previous test is its
    # own idea of what is in use
    for d in (paths.RELEASES, paths.AI_BIN, paths.ROLLBACK_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    cur = _build_dir("llama.cpp-b1000")
    paths.RELEASES_JSON.write_text(json.dumps(
        {"current": "llama.cpp-b1000",
         "releases": [{"name": "llama.cpp-b1000", "commit": "a" * 40,
                       "path": str(cur), "built_at": "2026-09-01T00:00:00+00:00"}]},
        indent=2) + "\n")
    (paths.AI_BIN / "llama-server").symlink_to(cur / "llama-server")
    db.init()
    db.execute("DELETE FROM jobs")
    yield LlamaCpp()
    db.execute("DELETE FROM jobs")
    paths.RELEASES_JSON.unlink(missing_ok=True)


def _running_job(kind: str = "llamacpp") -> None:
    db.execute("INSERT INTO jobs (kind, target, status, started_at) "
               "VALUES (?, 'HEAD', 'running', ?)", (kind, time.time()))


# ---------------------------------------------------------------- detection

def test_a_build_the_registry_never_recorded_is_found(home):
    """The whole point: it exists on disk and appears in nothing else."""
    stray = _build_dir("llama.cpp-b1042")
    out = llamacpp.stray_releases()
    assert [s["name"] for s in out["strays"]] == ["llama.cpp-b1042"]
    s = out["strays"][0]
    assert s["bytes"] >= 15 * MB and s["path"] == str(stray)
    assert s["purgeable"] is True and s["why_not"] is None
    assert out["bytes"] >= 15 * MB


def test_a_registered_build_is_never_reported(home):
    _build_dir("llama.cpp-b1042")
    names = [s["name"] for s in llamacpp.stray_releases()["strays"]]
    assert "llama.cpp-b1000" not in names, "the running build must never be offered"


def test_a_directory_the_portal_would_never_build_is_reported_but_not_offered(home):
    """Something else lives in ~/ai/releases. Say so; never offer to delete it."""
    (paths.RELEASES / "scratch-notes").mkdir(parents=True, exist_ok=True)
    out = llamacpp.stray_releases()
    assert [u["name"] for u in out["unexpected"]] == ["scratch-notes"]
    assert out["strays"] == []
    with pytest.raises(ValueError) as e:
        home.purge_stray("scratch-notes")
    assert "not the name of a build directory" in str(e.value)
    assert (paths.RELEASES / "scratch-notes").is_dir()


def test_a_stray_newer_than_the_running_build_is_flagged(home):
    """That one is not cruft — it is an update that built and never deployed."""
    _build_dir("llama.cpp-b1042")
    s = llamacpp.stray_releases()["strays"][0]
    assert s["build"] == 1042 and s["ahead"] is True


def test_no_releases_directory_is_not_an_error(home):
    shutil.rmtree(paths.RELEASES)
    assert llamacpp.stray_releases() == {
        "strays": [], "unexpected": [], "bytes": 0, "scanned": False}


# ------------------------------------------------------- what makes it in use

def test_a_build_the_bin_farm_points_at_is_in_use(home):
    """The farm, not the registry, is what the router executes.

    The case the registry cannot see: a hand-edited link, or a swap that
    recorded nothing, leaving a build running production either way.
    """
    """Two kinds of link, and both hold it: one whose target exists, and a broken
    one. A dangling link is still a declaration that this build is the one the
    router runs — and deleting the directory behind it makes the
    misconfiguration unrecoverable and erases why the link existed.
    """
    stray = _build_dir("llama.cpp-b1043")
    (paths.AI_BIN / "llama-server-live").symlink_to(stray / "llama-server")
    (paths.AI_BIN / "llama-cli").symlink_to(stray / "llama-cli")   # dangling
    s = llamacpp.stray_releases()["strays"][0]
    assert s["held_by"] == ["llama-cli", "llama-server-live"]
    assert s["purgeable"] is False and "~/ai/bin" in s["why_not"]
    assert "broken link" in s["why_not"], "naming the link is the actionable half"
    with pytest.raises(ValueError) as e:
        home.purge_stray("llama.cpp-b1043")
    assert "in use" in str(e.value)
    assert stray.is_dir(), "a refusal must not delete"


def test_a_build_a_process_is_running_from_is_in_use(home, monkeypatch):
    _build_dir("llama.cpp-b1044")
    monkeypatch.setattr(llamacpp, "_procs_using", lambda d: [4242])
    s = llamacpp.stray_releases()["strays"][0]
    assert s["purgeable"] is False and "4242" in s["why_not"]
    with pytest.raises(ValueError) as e:
        home.purge_stray("llama.cpp-b1044")
    assert "4242" in str(e.value)


def test_no_readable_proc_is_not_evidence_of_safety(home, monkeypatch):
    """"Couldn't check" must never be read as "nothing is running"."""
    stray = _build_dir("llama.cpp-b1045")
    monkeypatch.setattr(llamacpp, "_procs_using", lambda d: None)
    s = llamacpp.stray_releases()["strays"][0]
    assert s["purgeable"] is False and "/proc" in s["why_not"]
    with pytest.raises(ValueError) as e:
        home.purge_stray("llama.cpp-b1045")
    assert "/proc" in str(e.value)
    assert stray.is_dir()


@pytest.mark.skipif(not Path("/bin/sleep").is_file(), reason="needs /bin/sleep")
def test_the_process_reader_finds_a_real_process_started_from_a_directory(tmp_path):
    """Not a stubbed answer: a process we started, found by reading /proc."""
    exe = tmp_path / "sleep"
    shutil.copy("/bin/sleep", exe)
    p = subprocess.Popen([str(exe), "30"])
    try:
        assert p.pid in llamacpp._procs_using(tmp_path)
        assert llamacpp._procs_using(tmp_path / "nowhere-is-here") == []
    finally:
        p.kill()
        p.wait()


def test_the_process_reader_ignores_processes_elsewhere(tmp_path):
    assert llamacpp._procs_using(tmp_path) == []


# ----------------------------------------------------------------- purging

def test_purge_deletes_and_records_what_was_there(home):
    """Deleting an unrecorded build must leave an answer to "what was that"."""
    stray = _build_dir("llama.cpp-b1046", files=3,
                       version="version: 0.4.1-dev (build 1046, commit beef007)")
    res = home.purge_stray("llama.cpp-b1046", log=lambda m: None)
    assert not stray.exists()
    assert res["freed_bytes"] >= 15 * MB and res["files"] == 4   # 3 libs + the exe
    assert "build 1046" in res["version"]
    note = Path(res["note_dir"])
    text = (note / "note.txt").read_text()
    assert "never in ~/ai/state/llama-releases.json" in text
    assert "build 1046" in text
    manifest = (note / "manifest.txt").read_text().split()
    assert "llama-server" in manifest and len(manifest) == 4


def test_purge_refuses_a_build_that_is_registered(home):
    """Registered builds go through `delete build`, which also forgets the entry.

    Two paths that both accept registered names is two ways to end up with a
    registry describing a directory that no longer exists.
    """
    with pytest.raises(ValueError) as e:
        home.purge_stray("llama.cpp-b1000")
    assert "registered" in str(e.value)
    assert (paths.RELEASES / "llama.cpp-b1000").is_dir()


@pytest.mark.parametrize("name", ["", "..", "../../..", "../llama.cpp-b1000",
                                  "/etc", "llama.cpp-b", "llama.cpp-b1046/",
                                  "LLAMA.CPP-B1046", "llama.cpp-b1046/.ssh"])
def test_purge_refuses_anything_that_is_not_a_build_directory(home, name):
    _build_dir("llama.cpp-b1046")
    with pytest.raises(ValueError):
        home.purge_stray(name)
    assert (paths.RELEASES / "llama.cpp-b1046").is_dir()
    assert Path("/etc").is_dir(), "…and nothing outside the releases dir touched"


def test_purge_refuses_while_an_update_is_running(home):
    """A build in flight could be staging into that very path."""
    _build_dir("llama.cpp-b1047")
    _running_job()
    with pytest.raises(ValueError) as e:
        home.purge_stray("llama.cpp-b1047")
    assert "update job is running" in str(e.value)
    assert (paths.RELEASES / "llama.cpp-b1047").is_dir()


def test_purge_of_a_directory_with_no_binary_still_records(home):
    """Half-written staging has no llama-server; that must not sink the purge."""
    d = paths.RELEASES / "llama.cpp-b1048"
    d.mkdir(parents=True)
    (d / "libggml.so").write_bytes(b"\0" * MB)
    res = home.purge_stray("llama.cpp-b1048", log=lambda m: None)
    assert not d.exists()
    assert "no llama-server" in (Path(res["note_dir"]) / "note.txt").read_text()


# ------------------------------------------------------------------- routes

def _client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


def test_purge_route_requires_confirmation(home):
    _build_dir("llama.cpp-b1049")
    r = _client().post("/api/updates/llamacpp/purge",
                       json={"release": "llama.cpp-b1049", "confirm": False})
    assert r.status_code == 400
    assert (paths.RELEASES / "llama.cpp-b1049").is_dir()


def test_purge_route_is_refused_while_a_disruptive_job_runs(home):
    _build_dir("llama.cpp-b1050")
    _running_job("comfyui")
    c = _client()
    r = c.post("/api/updates/llamacpp/purge",
               json={"release": "llama.cpp-b1050", "confirm": True})
    assert r.status_code == 409
    assert "ComfyUI" in r.json()["detail"], "the refusal must name the busy job"
    assert (paths.RELEASES / "llama.cpp-b1050").is_dir()
    c.get("/api/jobs/active")          # and the machine reports itself busy


def test_purge_route_deletes_and_leaves_an_audit_event(home):
    _build_dir("llama.cpp-b1051")
    r = _client().post("/api/updates/llamacpp/purge",
                       json={"release": "llama.cpp-b1051", "confirm": True})
    assert r.status_code == 200, r.text
    assert not (paths.RELEASES / "llama.cpp-b1051").exists()
    ev = db.query("SELECT detail FROM events WHERE kind='update' "
                  "ORDER BY id DESC LIMIT 1")[0]["detail"]
    assert "UNREGISTERED build llama.cpp-b1051" in ev


def test_a_failing_stray_scan_does_not_take_the_release_list_down(home, monkeypatch):
    """Whatever else is wrong, the pane must still let you switch builds — and
    say it could not check, rather than silently showing "nothing to purge"."""
    def boom(*a, **kw):
        raise RuntimeError("releases dir exploded")
    monkeypatch.setattr(llamacpp, "stray_releases", boom)
    body = _client().get("/api/updates/llamacpp").json()
    assert body["release_count"] == 1, "the build list still works"
    assert body["strays"] == []
    assert "exploded" in (body["stray_error"] or "")


def test_advisory_names_the_build_and_where_to_act(home):
    _build_dir("llama.cpp-b1052")
    adv = [a for a in health.advisories(builds=llamacpp.stray_releases())
           if "llama.cpp-b1052" in a["text"]]
    assert len(adv) == 1 and adv[0]["level"] == "warn"
    assert adv[0]["where"] == "/#update-llamacpp"
    assert "newer than the running build" in adv[0]["text"]


def test_advisory_for_a_stray_older_than_the_running_build(home):
    """Different message: this one is cruft, not an interrupted deploy."""
    _build_dir("llama.cpp-b0001")
    adv = [a for a in health.advisories(builds=llamacpp.stray_releases())
           if "llama.cpp-b0001" in a["text"]]
    assert adv and "nothing lists them" in adv[0]["text"].lower()
    assert "newer than the running build" not in adv[0]["text"]


def test_no_strays_no_advisory(home):
    out = llamacpp.stray_releases()
    assert out["strays"] == []
    assert not [a for a in health.advisories(builds=out)
                if "never recorded" in a["text"] or "never deployed" in a["text"]]
