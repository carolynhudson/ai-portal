"""llama API-key audit: probe classification, hub-canonical honesty.

No live routers, no live hub: the probes and the hub read are injected
(the tests-never-touch-live-infra law). The classification IS the
product here: 'open' (serves without a key) is the state halo's router
lives in today, and the page must name it.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-key-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import llamakey, meshhub  # noqa: E402
from app.main import app  # noqa: E402


class Resp:
    def __init__(self, code):
        self.status_code = code


@pytest.fixture()
def probe_env(monkeypatch):
    """Inject the transport: get(url, headers=..) -> Resp per (url, with-key)."""
    def set_codes(with_key=200, no_key=401):
        def fake_get(url, timeout=0, headers=None):
            return Resp(with_key if headers else no_key)
        monkeypatch.setattr(llamakey.httpx, "get", fake_get)

    def set_raise():
        def fake_get(url, timeout=0, headers=None):
            raise RuntimeError("no route")
        monkeypatch.setattr(llamakey.httpx, "get", fake_get)
    return set_codes, set_raise


def test_classify_guarded_open_mismatch(probe_env):
    set_codes, set_raise = probe_env
    set_codes(200, 401)
    assert llamakey._probe("http://r:8080", "K")["state"] == "guarded"
    set_codes(200, 200)
    assert llamakey._probe("http://r:8080", "K")["state"] == "open"
    set_codes(401, 401)
    assert llamakey._probe("http://r:8080", "K")["state"] == "guarded-mismatch"
    set_codes(200, 403)
    assert "odd" in llamakey._probe("http://r:8080", "K")["state"]
    set_raise()
    assert llamakey._probe("http://r:8080", "K")["state"] == "down"


def test_canonical_hub_states(monkeypatch):
    monkeypatch.setattr(meshhub, "get_text", lambda n: "llama_abc\n")
    assert llamakey.canonical() == ("llama_abc", "hub")
    monkeypatch.setattr(meshhub, "get_text", lambda n: "")
    assert llamakey.canonical() == ("", "none")

    def boom(n):
        raise meshhub.MeshHubError("hub down")
    monkeypatch.setattr(meshhub, "get_text", boom)
    assert llamakey.canonical() == ("", "hub-down")


def test_bundle_aggregates(monkeypatch, probe_env):
    monkeypatch.setattr(meshhub, "get_text", lambda n: "llama_x" * 8)
    monkeypatch.setattr(llamakey, "_probe",
                        lambda url, key: {"url": url, "with_key": 200,
                                          "no_key": 401, "state": "guarded"})
    monkeypatch.setattr(llamakey, "_mesh_cache", (0.0, {}))
    b = llamakey.bundle(force=True)
    assert b["key_src"] == "hub" and b["key_len"] == len("llama_x" * 8)
    nodes = {r["node"] for r in b["routers"]}
    assert "halo" in nodes and "forge" in nodes
    assert all(r["state"] == "guarded" for r in b["routers"])


def test_page_renders_the_key_card(monkeypatch):
    monkeypatch.setattr(llamakey, "bundle", lambda force=False: {
        "key_src": "hub", "key_len": 47, "policy": "one key, hub-canonical",
        "routers": [{"node": "halo", "url": "http://h:8080", "with_key": 200,
                     "no_key": 200, "state": "open"},
                    {"node": "forge", "url": "http://f:8082", "with_key": 200,
                     "no_key": 401, "state": "guarded"}]})
    c = TestClient(app)
    assert c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
                  follow_redirects=False).status_code == 302
    t = c.get("/mesh").text
    assert "llama API key (mesh routers)" in t
    assert "OPEN (unguarded)" in t      # the hazard state, named
    assert "guarded" in t and "llama-api-key" in t
