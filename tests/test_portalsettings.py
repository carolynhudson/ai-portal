"""The projects root — the one portal setting that says where an agent may write (§6.14.1n).

`portalsettings.validate_projects_root` is read-only and answers with a reason, so the tests are
about *which* values are refused and whether the refusal is the fact a person can act on. Every
test works in a temporary home; the operator's real `~/ai` tree is never touched — `home()` and
`portal_home()` are patched, which is also the seam the tests use to place a path outside the home
and a path inside the portal's own home.
"""
from __future__ import annotations

import os

import pytest

from app.core import portalsettings


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A home directory, a portal home under it, and nothing else."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "ai").mkdir()
    monkeypatch.setattr(portalsettings, "home", lambda: str(home))
    monkeypatch.setattr(portalsettings, "portal_home", lambda: str(home / "ai"))
    return home


# --- the accept/refusal line ---------------------------------------------------------------

def test_the_default_is_the_folder_the_operator_named(sandbox, monkeypatch):
    monkeypatch.setenv("HOME", str(sandbox))
    v = portalsettings.validate_projects_root("~/ai-projects")
    assert v["ok"], v
    assert v["path"] == os.path.realpath(str(sandbox / "ai-projects"))
    assert v["will_create"] is True and v["exists"] is False, "a missing folder is fine; creation is a separate ask"


def test_a_path_that_already_exists_is_accepted_and_reported_as_existing(sandbox):
    target = sandbox / "code"
    target.mkdir()
    v = portalsettings.validate_projects_root(str(target))
    assert v["ok"] and v["exists"] is True and v.get("will_create") is False


def test_a_relative_path_is_refused_because_it_depends_on_where_the_service_started(sandbox):
    v = portalsettings.validate_projects_root("ai-projects")
    assert not v["ok"]
    assert "relative" in v["error"] and "~/ai-projects" in v["error"], "the refusal names the fix"


def test_a_path_outside_the_home_is_refused_and_names_the_home(sandbox):
    outside = str(sandbox.parent / "anywhere-projects")
    v = portalsettings.validate_projects_root(outside)
    assert not v["ok"]
    assert os.path.realpath(outside) in v["error"]
    assert os.path.realpath(str(sandbox)) in v["error"], "the operator is told what the portal considers reachable"


def test_the_portal_home_itself_and_anything_inside_it_are_refused(sandbox):
    inside = portalsettings.validate_projects_root(str(sandbox / "ai" / "projects"))
    assert not inside["ok"] and "portal's own home" in inside["error"]

    exact = portalsettings.validate_projects_root(str(sandbox / "ai"))
    assert not exact["ok"] and "model library" in exact["error"]


def test_a_path_escaping_the_home_through_dotdot_is_refused(sandbox):
    v = portalsettings.validate_projects_root("~/../outside")
    assert not v["ok"] and "outside your home" in v["error"]


def test_an_existing_file_is_refused_as_what_it_is(sandbox):
    f = sandbox / "notes.txt"
    f.write_text("not a folder")
    v = portalsettings.validate_projects_root(str(f))
    assert not v["ok"] and "is not a directory" in v["error"]


def test_a_file_as_the_ancestor_of_a_missing_folder_is_refused(sandbox):
    f = sandbox / "notes.txt"
    f.write_text("not a folder")
    v = portalsettings.validate_projects_root(str(f / "projects"))
    assert not v["ok"]
    assert str(f) in v["error"], "the refusal names the ancestor that blocks it, not the leaf that does not exist"


def test_an_unwriteable_ancestor_is_refused_before_the_setting_is_saved(sandbox):
    locked = sandbox / "locked"
    locked.mkdir(mode=0o500)
    v = portalsettings.validate_projects_root(str(locked / "projects"))
    assert not v["ok"] and "not writable" in v["error"], "a save that succeeds and a create that never does is the failure this prevents"
    locked.chmod(0o700)


def test_a_non_string_is_refused_without_a_traceback(sandbox):
    for value in (None, 7, ["~/ai-projects"], {"path": "~/ai-projects"}):
        v = portalsettings.validate_projects_root(value)
        assert not v["ok"] and "path string" in v["error"]


def test_an_empty_string_is_refused(sandbox):
    assert "empty" in portalsettings.validate_projects_root("   ")["error"]


def test_a_null_byte_is_refused(sandbox):
    assert "null byte" in portalsettings.validate_projects_root("~/ai-projects\x00")["error"]


def test_deep_traversal_is_refused(sandbox):
    v = portalsettings.validate_projects_root("~/ai-projects/" + "../" * 40 + "x")
    assert not v["ok"] and "traversal" in v["error"]


# --- storing it ---------------------------------------------------------------------------

def test_an_unknown_key_is_refused_and_names_the_keys_the_page_can_write(sandbox, monkeypatch):
    monkeypatch.setattr(portalsettings.db, "set_setting", lambda *a, **k: pytest.fail("a refusal must not write"))
    out = portalsettings.set_value("router_poll_ms", 10)
    assert not out["ok"]
    assert "projects_root" in out["error"], "the answer says what the page can write, not just no"


def test_an_accepted_value_is_stored_and_read_back_as_the_same_string(sandbox, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(portalsettings.db, "set_setting", lambda k, v: store.__setitem__(k, str(v)))
    monkeypatch.setattr(portalsettings.db, "get_setting", lambda k, default=None: store.get(k, default))

    target = sandbox / "work"
    out = portalsettings.set_value("projects_root", f"  {target}  ")
    assert out["ok"], out
    assert store["projects_root"] == str(target), "the stray spaces are trimmed at the boundary, not stored"
    assert portalsettings.projects_root() == os.path.realpath(str(target))


def test_a_refused_value_never_reaches_the_store(sandbox, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(portalsettings.db, "set_setting", lambda k, v: store.__setitem__(k, str(v)))
    out = portalsettings.set_value("projects_root", str(sandbox.parent / "nope"))
    assert not out["ok"] and store == {}


def test_view_reports_the_effective_value_and_the_path_it_resolves_to(sandbox, monkeypatch):
    monkeypatch.setattr(portalsettings.db, "current_settings", lambda: {"projects_root": "~/ai-projects"})
    monkeypatch.setenv("HOME", str(sandbox))
    v = portalsettings.view()["projects_root"]
    assert v["kind"] == "text" and v["default"] == "~/ai-projects"
    assert v["path"] == os.path.realpath(str(sandbox / "ai-projects"))
    assert v["exists"] is False
    assert "folder" in v["help"]


# --- the endpoints (D1: every read is also an endpoint) ------------------------------------

def _client():
    from fastapi.testclient import TestClient

    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_the_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.get("/api/settings/portal", follow_redirects=False).status_code == 401
    assert c.post("/api/settings/portal", json={"key": "projects_root", "value": "~/x"},
                  follow_redirects=False).status_code == 401
    assert c.post("/api/settings/portal/validate", json={"key": "projects_root", "value": "~/x"},
                  follow_redirects=False).status_code == 401


def test_the_read_endpoint_answers_the_page():
    c = _client()
    r = c.get("/api/settings/portal")
    assert r.status_code == 200, r.text
    row = r.json()["projects_root"]
    for key in ("kind", "default", "help", "value", "path", "exists"):
        assert key in row, key


def test_validate_answers_without_saving(sandbox, monkeypatch):
    monkeypatch.setattr(portalsettings.db, "set_setting", lambda *a, **k: pytest.fail("validate must not write"))
    c = _client()
    r = c.post("/api/settings/portal/validate", json={"key": "projects_root", "value": "relative/path"})
    assert r.status_code == 200, "a refusal is an answer, not an HTTP error"
    assert r.json()["ok"] is False and "relative" in r.json()["error"]


def test_the_write_endpoint_refuses_with_the_reason_the_page_shows(sandbox):
    c = _client()
    outside = str(sandbox.parent / "nope")
    r = c.post("/api/settings/portal", json={"key": "projects_root", "value": outside})
    assert r.status_code == 409, r.text
    assert "outside your home" in r.json()["detail"]


def test_the_write_endpoint_refuses_an_unknown_key_by_name():
    c = _client()
    r = c.post("/api/settings/portal", json={"key": "secrets_path", "value": "~/x"})
    assert r.status_code == 409
    assert "secrets_path" in r.json()["detail"] and "projects_root" in r.json()["detail"]


def test_the_write_endpoint_accepts_a_path_inside_the_home(sandbox, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(portalsettings.db, "set_setting", lambda k, v: store.__setitem__(k, str(v)))
    monkeypatch.setattr(portalsettings.db, "get_setting", lambda k, default=None: store.get(k, default))
    target = sandbox / "projects"
    c = _client()
    r = c.post("/api/settings/portal", json={"key": "projects_root", "value": str(target)})
    assert r.status_code == 200, r.text
    assert store["projects_root"] == str(target), "the endpoint stores the string; db.set_setting does the encoding"


def test_projects_root_is_in_the_settings_the_dashboard_reads():
    c = _client()
    assert c.get("/api/settings").json()["projects_root"] == "~/ai-projects"
