"""mesh.status aggregation: lane degradation, token provenance, caching.

All probes are injected; no network, no hub, no forge. The live view is
the running portal's job, not the suite's.
"""
import os
import tempfile
import time

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-mst-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import meshhub, meshstatus  # noqa: E402
from app.main import app  # noqa: E402


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


@pytest.fixture()
def lanes(monkeypatch):
    """Replace every probe with a canned lane; the TTL cache starts cold."""
    monkeypatch.setattr(meshstatus, "_cache", (0.0, {}))

    def set(hub=None, router=None, forge=None, fr=None, meshd=None):
        """Defaults are inert: no lane may reach a real host from the suite.
        Lane values may be callables (they get called per probe) or plain
        dicts (the canned answer)."""
        inert = {"ok": False, "error": "not probed (test)", "token_src": "none"}

        def val(v, fallback):
            return v() if callable(v) else (fallback if v is None else v)

        monkeypatch.setattr(meshstatus.meshhub, "nodes", lambda: hub if hub is not None else [])
        monkeypatch.setattr(meshstatus, "_router",
                            lambda: val(router, {"ok": False, "error": "not probed (test)"}))
        monkeypatch.setattr(meshstatus, "_forge",
                            lambda: val(forge, dict(inert, url="u")))
        if fr is not None:
            fr.setdefault("ok", True)
            fr.setdefault("token_src", "hub")
        monkeypatch.setattr(meshstatus, "_forge_router",
                            lambda _a: val(fr, dict(inert, url="u")))
        monkeypatch.setattr(meshstatus, "_meshd",
                            lambda: val(meshd, dict(inert, url="u")))
    return set


def test_all_down_renders_reasons(monkeypatch):
    monkeypatch.setattr(meshstatus, "_cache", (0.0, {}))
    monkeypatch.setattr(meshstatus.meshhub, "nodes",
                        _raise(meshhub.MeshHubError("meshctl not installed")))
    monkeypatch.setattr(meshstatus, "_router", lambda: {"ok": False, "error": "router unreachable"})
    monkeypatch.setattr(meshstatus, "_forge",
                        lambda: {"ok": False, "error": "ConnectError", "url": "http://f:8188"})
    monkeypatch.setattr(meshstatus, "_forge_router",
                        lambda _a: {"ok": False, "error": "ConnectError",
                                    "url": "http://f:8082", "token_src": "none"})
    monkeypatch.setattr(meshstatus, "_meshd",
                        lambda: {"ok": False, "error": "ConnectError", "url": "http://f:8092"})
    b = meshstatus.bundle(force=True)
    assert b["hub"]["ok"] is False and "not installed" in b["hub"]["error"]
    assert b["router"]["ok"] is False and b["forge"]["ok"] is False
    assert b["forge_router"]["ok"] is False and b["token_src"] == "none"


def test_hub_ok_nodes_get_age_bubbles(lanes):
    now = time.time()
    lanes(hub=[{"name": "halo", "fp": "a" * 64, "agepub": "age1" + "x" * 40,
                "roles": ["portal"], "last": now - 30},
               {"name": "forge", "fp": "b" * 64, "agepub": "age1" + "y" * 40,
                "roles": ["forge"], "last": now - 7200}],
          router={"ok": True, "models": ["m1"]},
          forge={"ok": True, "url": "http://f:8188", "version": "1.23"},
          fr={"ok": True, "url": "http://f:8082", "models": ["qwen"]})
    b = meshstatus.bundle(force=True)
    assert b["hub"]["ok"]
    ages = {n["name"]: n["ago"] for n in b["hub"]["nodes"]}
    assert ages["halo"].endswith("s") and ages["forge"].endswith("h")


def test_token_provenance_hub_first(monkeypatch):
    monkeypatch.setattr(meshstatus.meshhub, "get_text", lambda n: "TOKEN-HUB\n")
    assert meshstatus._token() == ("TOKEN-HUB", "hub")


def test_token_provenance_file_fallback(tmp_path, monkeypatch):
    f = tmp_path / "subagent_key.txt"
    f.write_text("TOKEN-FILE\n")
    monkeypatch.setattr(meshstatus.meshhub, "get_text",
                        _raise(meshhub.MeshHubError("hub down")))
    monkeypatch.setattr(meshstatus, "_TOK_FILE", f)
    assert meshstatus._token() == ("TOKEN-FILE", "file")


def test_token_provenance_none(tmp_path, monkeypatch):
    monkeypatch.setattr(meshstatus.meshhub, "get_text",
                        _raise(meshhub.MeshHubError("hub down")))
    monkeypatch.setattr(meshstatus, "_TOK_FILE", tmp_path / "nope.txt")
    assert meshstatus._token() == ("", "none")


def test_forge_router_url_derivation(monkeypatch):
    monkeypatch.setattr(meshstatus, "_forge_base",
                        lambda: "http://RainbowBlock.local:8188")
    monkeypatch.delenv("AIPP_FORGE_ROUTER_URL", raising=False)
    assert meshstatus._forge_router_url() == "http://RainbowBlock.local:8082"
    monkeypatch.setenv("AIPP_FORGE_ROUTER_URL", "http://10.0.0.5:9000")
    assert meshstatus._forge_router_url() == "http://10.0.0.5:9000"


def test_bundle_cache(lanes, monkeypatch):
    calls = {"n": 0}

    def count_router():
        calls["n"] += 1
        return {"ok": True, "models": []}
    lanes(hub=[], router=count_router, forge={"ok": True, "url": "u", "version": None},
          fr={"ok": True, "url": "u", "models": []})
    meshstatus.bundle(force=True)
    meshstatus.bundle()
    meshstatus.bundle()
    assert calls["n"] == 1


# ---------------------------------------------------------------- leaf expiry

def _openssl_or_skip():
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("openssl not on PATH")
    return True


def test_leaf_parses_real_cert(tmp_path, monkeypatch):
    import subprocess
    _openssl_or_skip()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ed25519", "-keyout",
                    str(tmp_path / "k.pem"), "-out", str(tmp_path / "cert.crt"),
                    "-days", "2", "-nodes", "-subj", "/CN=leaf-test"],
                   check=True, capture_output=True)
    (tmp_path / "config.json").write_text('{"cert": "cert.crt"}')
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    leaf = meshstatus._leaf()
    assert leaf["ok"] and 40 < leaf["hours"] < 50  # -days 2 => ~48h
    assert leaf["notAfter"]


def test_leaf_degrades_without_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_DIR", str(tmp_path / "absent"))
    leaf = meshstatus._leaf()
    assert leaf["ok"] is False and "FileNotFound" in leaf["error"]


def test_leaf_in_bundle_and_page(client, lanes, monkeypatch):
    monkeypatch.setattr(meshstatus, "_leaf",
                        lambda: {"ok": True, "hours": 3.2, "notAfter": "2026-11-16 05:33 PST"})
    lanes(hub=[{"name": "halo", "fp": "a" * 64, "agepub": "age1" + "q" * 40,
                "roles": ["portal"], "last": time.time()}],
          router={"ok": True, "models": []},
          forge={"ok": True, "url": "u", "version": None},
          fr={"ok": True, "url": "u", "models": []})
    b = meshstatus.bundle(force=True)
    assert b["leaf"]["ok"] and b["leaf"]["hours"] == 3.2
    r = client.get("/mesh")
    assert "node leaf:" in r.text and "expires in 3.2 h" in r.text
    assert "ms-warn" in r.text  # <6h paints the countdown amber


def test_leaf_degraded_text_renders(client, lanes, monkeypatch):
    monkeypatch.setattr(meshstatus, "_leaf",
                        lambda: {"ok": False, "error": "FileNotFoundError: no cert"})
    lanes(hub=[], router={"ok": True, "models": []},
          forge={"ok": True, "url": "u", "version": None},
          fr={"ok": True, "url": "u", "models": []})
    r = client.get("/mesh")
    assert "node leaf:" in r.text and "no cert" in r.text


def test_meshd_url_derivation(monkeypatch):
    monkeypatch.setattr(meshstatus, "_forge_base",
                        lambda: "http://RainbowBlock.local:8188")
    monkeypatch.delenv("AIPP_FORGE_MESHD_URL", raising=False)
    assert meshstatus._meshd_url() == "http://RainbowBlock.local:8092"
    monkeypatch.setenv("AIPP_FORGE_MESHD_URL", "http://10.0.0.9:9123")
    assert meshstatus._meshd_url() == "http://10.0.0.9:9123"


def test_meshd_card_live_shape(client, lanes):
    lanes(hub=[], router={"ok": True, "models": []},
          forge={"ok": True, "url": "u", "version": None},
          fr={"ok": True, "url": "u", "models": []},
          meshd={"ok": True, "url": "http://f:8092", "node": "rainbowblock",
                 "version": "0.3.0", "phase": "2 (status + leases + service control)",
                 "verdict": "safe", "vram": {"total": 11264, "used": 2262},
                 "ram": {"total_mib": 64628, "used_mib": 16346}, "uptime_s": 350296})
    r = client.get("/mesh")
    assert "meshd (node daemon)" in r.text
    assert "meshd 0.3.0" in r.text and "safe" in r.text
    assert "VRAM 2262/11264 MiB" in r.text and "RAM 16346/64628 MiB" in r.text
    assert "up 4.1 d" in r.text


def test_meshd_card_degrades(client, lanes):
    lanes(hub=[], router={"ok": True, "models": []},
          forge={"ok": True, "url": "u", "version": None},
          fr={"ok": True, "url": "u", "models": []},
          meshd={"ok": False, "url": "http://f:8092", "error": "ConnectError"})
    r = client.get("/mesh")
    assert "meshd (node daemon)" in r.text and "ConnectError" in r.text


@pytest.fixture()
def client():
    c = TestClient(app)
    r = c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
               follow_redirects=False)
    assert r.status_code == 302
    return c


def test_page_renders(client, lanes):
    lanes(hub=[{"name": "halo", "fp": "a" * 64, "agepub": "age1" + "z" * 40,
                "roles": ["portal"], "last": time.time()}],
          router={"ok": True, "models": ["test-model"]},
          forge={"ok": False, "error": "unreachable", "url": "http://f:8188"},
          fr={"ok": True, "url": "http://f:8082", "models": ["forge-model"]})
    r = client.get("/mesh")
    assert r.status_code == 200
    assert "Hub (Pi pair registry)" in r.text
    assert "test-model" in r.text and "forge-model" in r.text
    assert "token: hub" in r.text
