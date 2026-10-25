"""The opencode update tab's read model (owner Oct-25).

Three versions, three facts — measured on the box: the RUNNING service keeps the
binary it started with, the DISK binary self-updates under it (opencode's
autoupdate), and the LATEST v2 tag is the channel (GitHub's `releases/latest`
answers the v1 line: v1.19.4 while v2.1.4 tags exist). Every network read in
here is monkeypatched; no test leaves the process, and the verbs (which do
restart things) live in main.py and are tested for their confirm gates here.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-harnessupd"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core import db, harnessctl, harnessupd  # noqa: E402
from app.main import app  # noqa: E402

GH_TAGS = [
    {"name": "v1.19.4"},          # the v1 line: never newer than v2, excluded
    {"name": "v2.1.4"},
    {"name": "v2.1.3"},
    {"name": "v2.0.32"},
]
PLUGIN_ROWS = [
    {"id": "opencode.tool.websearch", "source": {"type": "builtin"},
     "state": {"status": "active"}},
    {"id": "opencode-search", "source": {"type": "package",
                                         "target": "opencode-search@1.0.0",
                                         "version": "1.0.0"},
     "state": {"status": "active"}},
]


def _fake_json_for(npm_latest="1.0.0"):
    def _get(url, timeout=10.0):
        if "github" in url:
            return GH_TAGS
        return {"dist-tags": {"latest": npm_latest}}
    return _get


def _live_host(monkeypatch):
    """The host answers /api/info and the plugin check, as measured."""
    def _request(method, path, body=None, port=None):
        if path == harnessctl.HEALTH_PATH:
            return 200, {"version": "2.0.32"}
        if path == "/api/plugin/check":
            return 200, {"data": PLUGIN_ROWS}
        raise AssertionError(f"unexpected {method} {path}")
    monkeypatch.setattr(harnessctl, "_request", _request)


def _disk(monkeypatch, tmp_path, version="2.1.4", rc=0):
    binp = tmp_path / "opencode"
    binp.write_text("#!/bin/sh\n")
    monkeypatch.setattr(harnessctl, "harness_bin", lambda: binp)
    monkeypatch.setattr(harnessupd.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": rc, "stdout": f"opencode v{version}", "stderr": ""})())
    return binp


@pytest.fixture
def clean_cache():
    harnessupd._CACHE.update(at=0.0, data=None)
    yield
    harnessupd._CACHE.update(at=0.0, data=None)


# ------------------------------------------------------------------ version math
def test_parse_ver_accepts_the_shapes_the_box_actually_prints():
    assert harnessupd.parse_ver("v2.1.4") == (2, 1, 4)
    assert harnessupd.parse_ver("2.1.4") == (2, 1, 4)
    assert harnessupd.parse_ver("opencode v2.1.4\n") == (2, 1, 4)
    assert harnessupd.parse_ver("no version here") is None, \
        "absence must not compare as version zero"


def test_newer_needs_both_sides():
    assert harnessupd.newer("2.1.4", "2.1.3")
    assert not harnessupd.newer("2.1.3", "2.1.4")
    assert not harnessupd.newer("2.1.4", "2.1.4")
    assert not harnessupd.newer(None, "2.1.4")
    assert not harnessupd.newer("2.1.4", None)


def test_latest_v2_is_the_highest_v2_tag_and_ignores_the_v1_line(monkeypatch):
    monkeypatch.setattr(harnessupd, "_get_json", _fake_json_for())
    ver, note = harnessupd.latest_v2()
    assert ver == "2.1.4" and note is None, \
        "releases/latest answers v1.19.4 (measured); tags are the v2 channel"


def test_latest_v2_states_a_failed_read_rather_than_reading_as_current(monkeypatch):
    def boom(url, timeout=10.0):
        raise OSError("network down")
    monkeypatch.setattr(harnessupd, "_get_json", boom)
    ver, note = harnessupd.latest_v2()
    assert ver is None and "failed" in note


def test_disk_version_is_a_stated_absence_when_the_binary_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(harnessctl, "harness_bin", lambda: tmp_path / "nope")
    ver, note = harnessupd.disk_version()
    assert ver is None and "no binary" in note


# ------------------------------------------------------------------ the payload
def test_check_reports_the_three_versions_as_the_measured_state(tmp_path, monkeypatch,
                                                               clean_cache):
    _live_host(monkeypatch)
    _disk(monkeypatch, tmp_path)
    monkeypatch.setattr(harnessupd, "_get_json", _fake_json_for())
    monkeypatch.setattr(harnessctl, "plugin_specs",
                        lambda: ["opencode-search@1.0.0"])
    d = harnessupd.check(refresh=True)
    oc = d["opencode"]
    assert oc["running"] == "2.0.32" and oc["disk"] == "2.1.4" and oc["latest"] == "2.1.4"
    assert oc["update_available"] is False, "disk is at latest"
    assert oc["restart_needed"] is True, "the running host lags the self-updated disk"
    p = d["plugins"][0]
    assert p["name"] == "opencode-search" and p["pinned"] == "1.0.0"
    assert p["loaded"] == "1.0.0" and p["status"] == "active"
    assert p["update_available"] is False


def test_check_flags_a_plugin_pin_behind_npm_latest(tmp_path, monkeypatch, clean_cache):
    _live_host(monkeypatch)
    _disk(monkeypatch, tmp_path)
    monkeypatch.setattr(harnessupd, "_get_json", _fake_json_for(npm_latest="1.1.0"))
    monkeypatch.setattr(harnessctl, "plugin_specs",
                        lambda: ["opencode-search@1.0.0"])
    d = harnessupd.check(refresh=True)
    assert d["plugins"][0]["update_available"] is True


def test_a_down_host_degrades_to_a_stated_unknown(tmp_path, monkeypatch, clean_cache):
    def down(method, path, body=None, port=None):
        raise harnessctl.HarnessError("the host is not answering")
    monkeypatch.setattr(harnessctl, "_request", down)
    _disk(monkeypatch, tmp_path)
    monkeypatch.setattr(harnessupd, "_get_json", _fake_json_for())
    monkeypatch.setattr(harnessctl, "plugin_specs", lambda: [])
    d = harnessupd.check(refresh=True)
    assert d["opencode"]["running"] is None
    assert "not answering" in d["opencode"]["running_note"]
    assert d["opencode"]["restart_needed"] is False, "unknown running is not 'behind'"


def test_check_answers_from_the_cache_between_reads(tmp_path, monkeypatch, clean_cache):
    calls = []
    def counting(url, timeout=10.0):
        calls.append(url)
        return _fake_json_for()(url, timeout)
    monkeypatch.setattr(harnessupd, "_get_json", counting)
    monkeypatch.setattr(harnessctl, "_request",
                        lambda *a, **k: (200, {"version": "2.0.32"}))
    monkeypatch.setattr(harnessctl, "plugin_specs", lambda: [])
    _disk(monkeypatch, tmp_path)
    harnessupd.check(refresh=True)
    second = harnessupd.check()
    assert second["cached"] is True
    assert len([c for c in calls if "github" in c]) == 1, "one GitHub read per hour"


# ------------------------------------------------------------------ plugin pins
def test_plugin_specs_reads_the_setting_and_refuses_unpinned():
    db.set_setting("harness_plugins", ["opencode-search@1.0.0", "@scope/pkg@2.3.4"])
    assert harnessctl.plugin_specs() == ["opencode-search@1.0.0", "@scope/pkg@2.3.4"]
    db.set_setting("harness_plugins", ["opencode-search"])   # floating latest
    with pytest.raises(harnessctl.HarnessError):
        harnessctl.plugin_specs()
    db.set_setting("harness_plugins", None)
    assert harnessctl.plugin_specs() == list(harnessctl.HARNESS_V2_PLUGINS), \
        "an unset list falls back to the pinned default"


# ------------------------------------------------------------------ the routes
@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]})
    return c


def test_the_tab_get_is_the_check(client, monkeypatch):
    monkeypatch.setattr(harnessupd, "check",
                        lambda refresh=False: {"opencode": {"running": "2.0.32"},
                                               "plugins": []})
    r = client.get("/api/updates/opencode")
    assert r.status_code == 200 and r.json()["opencode"]["running"] == "2.0.32"


def test_every_verb_refuses_without_confirm(client):
    for verb in ("upgrade", "restart", "plugins"):
        r = client.post(f"/api/updates/opencode/{verb}", json={})
        assert r.status_code == 409, verb
        assert "confirm" in str(r.json()), verb


def test_the_upgrade_verb_runs_the_official_updater_and_says_restart_comes_next(
        client, tmp_path, monkeypatch):
    binp = tmp_path / "opencode"
    binp.write_text("#!/bin/sh\n")
    monkeypatch.setattr(harnessctl, "harness_bin", lambda: binp)
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return type("P", (), {"returncode": 0, "stdout": "upgraded", "stderr": ""})()
    monkeypatch.setattr(main_mod.subprocess, "run", run)
    r = client.post("/api/updates/opencode/upgrade", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert seen["argv"] == [str(binp), "upgrade"], "opencode's own updater, not a pip/npm guess"
    assert r.json()["restart_needed"] is True, "the running host keeps its version"


def test_the_upgrade_verb_names_a_missing_binary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(harnessctl, "harness_bin", lambda: tmp_path / "gone")
    r = client.post("/api/updates/opencode/upgrade", json={"confirm": True})
    assert r.status_code == 409 and "no harness binary" in str(r.json())


def test_the_restart_verb_goes_through_the_managed_unit(client, monkeypatch):
    seen = {}
    def act(key, action):
        seen.update(key=key, action=action)
        return {"ok": True, "state": {"ActiveState": "active"}}
    monkeypatch.setattr(main_mod.systemd, "service_action", act)
    r = client.post("/api/updates/opencode/restart", json={"confirm": True})
    assert r.status_code == 200 and seen == {"key": "harness", "action": "restart"}


def test_the_plugin_verb_moves_pins_to_latest_and_resyncs(client, monkeypatch):
    db.set_setting("harness_plugins", ["opencode-search@1.0.0"])
    monkeypatch.setattr(harnessupd, "check", lambda refresh=False: {
        "plugins": [{"name": "opencode-search", "pinned": "1.0.0",
                     "latest": "1.1.0", "update_available": True}]})
    synced = {}
    def sync(port=None, log=None):
        synced["port"] = port
        synced["specs"] = harnessctl.plugin_specs()
        return {"ok": True}
    monkeypatch.setattr(harnessctl, "sync", sync)
    r = client.post("/api/updates/opencode/plugins", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == ["opencode-search@1.1.0"]
    assert synced["specs"] == ["opencode-search@1.1.0"], \
        "the seed reads the same setting: the pins it writes are the pins that load"
    assert db.get_setting("harness_plugins") == ["opencode-search@1.1.0"]
    db.set_setting("harness_plugins", None)


def test_the_plugin_verb_says_so_when_everything_is_current(client, monkeypatch):
    db.set_setting("harness_plugins", ["opencode-search@1.0.0"])
    monkeypatch.setattr(harnessupd, "check", lambda refresh=False: {
        "plugins": [{"name": "opencode-search", "pinned": "1.0.0",
                     "latest": "1.0.0", "update_available": False}]})
    r = client.post("/api/updates/opencode/plugins", json={"confirm": True})
    assert r.status_code == 200 and r.json()["changed"] is False
    db.set_setting("harness_plugins", None)
