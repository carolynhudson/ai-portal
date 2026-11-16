"""mesh.place: card aggregation, the owner's placement policy, degradation.

The MCP transport is injected -- the suite never touches a live door.
The policy under test is the OWNER's (memory 210): the forge is a
minor-assistant node, so cuda-heavy cards there rank below idle non-cuda
cards; the marker must be visible data, not a hidden bias.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-plc-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import meshplace, meshstatus  # noqa: E402
from app.main import app  # noqa: E402

CAPS = {
    "node": "rainbowblock",
    "cards": [
        {"service": "birefnet_cutout", "kind": "image-forge", "fills": ["image"],
         "requires": {"backend": "cuda", "vram_gb_min": 4}, "state": "idle",
         "limits": {"concurrency": 1, "timeout_s": [30, 1800]}, "node": "rainbowblock"},
        {"service": "svg_writer_llm", "kind": "llm-router", "fills": ["text"],
         "requires": {"backend": "cpu", "vram_gb_min": 6}, "state": "idle",
         "limits": {"concurrency": 1, "timeout_s": [30, 600]}, "node": "rainbowblock"},
        {"service": "z_image", "kind": "image-forge", "fills": ["image"],
         "requires": {"backend": "cuda", "vram_gb_min": 8}, "state": "rendering",
         "limits": {"concurrency": 1, "timeout_s": [30, 1800]}, "node": "rainbowblock"},
    ],
}

LEASES = {"leases": {"count": 2, "committed_vram_mib": 4096, "committed_ram_mib": 0,
          "leases": [{"id": "abc", "job": "offload qwen3-4b", "vram_mib": 4096, "ttl_s": 300},
                     {"id": "def", "job": "render", "vram_mib": 0}]}}


@pytest.fixture()
def doors(monkeypatch):
    monkeypatch.setattr(meshplace, "_cache", (0.0, {}))
    monkeypatch.setattr(meshplace, "_doors", lambda: ["http://f:8092"])
    monkeypatch.setattr(meshstatus, "_token", lambda: ("TOK", "hub"))

    def set_caps(caps=CAPS, leases=LEASES):
        def call(u, t, a, tok):
            return caps if t == "node_caps" else leases
        monkeypatch.setattr(meshplace, "mcp_tool", call)

    def set_raise(exc):
        def boom(*a, **k):
            raise exc
        monkeypatch.setattr(meshplace, "mcp_tool", boom)
    return set_caps, set_raise, monkeypatch


def test_cards_and_policy_ranking(doors):
    set_caps, _, _ = doors
    set_caps()
    b = meshplace.bundle(force=True)
    assert b["cards"] == 3 and b["token_src"] == "hub"
    cards = {c["service"]: c for c in b["nodes"][0]["cards"]}
    # minor-assistant policy (mem 210): idle cuda card is deprioritized to
    # MED, while an idle CPU card ranks HIGH -- visible, named, not hidden.
    assert cards["birefnet_cutout"]["pref"] == "med"
    assert cards["svg_writer_llm"]["pref"] == "high"
    assert cards["z_image"]["pref"] == "low"      # rendering, cuda, minor node
    assert "minor-assistant" in b["nodes"][0]["policy"]


def test_door_degrades_with_reason(doors):
    _, set_raise, _ = doors
    set_raise(RuntimeError("door down"))
    b = meshplace.bundle(force=True)
    assert b["cards"] == 0
    assert b["nodes"][0]["ok"] is False and "door down" in b["nodes"][0]["error"]


def test_no_token_is_honest(doors, monkeypatch):
    monkeypatch.setattr(meshstatus, "_token", lambda: ("", "none"))
    b = meshplace.bundle(force=True)
    assert b["cards"] == 0 and "no ONE-token" in b["nodes"][0]["error"]


def test_cache(doors):
    set_caps, _, monkeypatch = doors
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return CAPS
    monkeypatch.setattr(meshplace, "mcp_tool", counted)
    meshplace.bundle(force=True)
    meshplace.bundle()
    meshplace.bundle()
    assert calls["n"] == 2  # two tools per door (caps + status), then cache holds


def test_leases_aggregate(doors):
    set_caps, _, _ = doors
    set_caps()
    b = meshplace.bundle(force=True)
    assert b["leased"] == 2 and b["committed_vram_mib"] == 4096


def test_mesh_status_failure_keeps_cards(doors):
    set_caps, _, monkeypatch = doors

    def call(u, t, a, tok):
        if t == "mesh_status":
            raise RuntimeError("status door down")
        return CAPS
    monkeypatch.setattr(meshplace, "mcp_tool", call)
    b = meshplace.bundle(force=True)
    n = b["nodes"][0]
    assert n["ok"] and len(n["cards"]) == 3          # cards survive
    assert "status door down" in n["leases"]["error"]  # the lane names its truth


def test_page_shows_lease_rows(doors):
    set_caps, _, _ = doors
    set_caps()
    c = TestClient(app)
    assert c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
                  follow_redirects=False).status_code == 302
    t = c.get("/place").text
    assert "leases 2 (4096 MiB committed)" in t
    assert "#abc offload qwen3-4b · 4096 MiB · ttl 300s" in t


def test_page_renders_live_and_degraded(doors, monkeypatch):
    set_caps, set_raise, _ = doors
    set_caps()
    c = TestClient(app)
    assert c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
                  follow_redirects=False).status_code == 302
    t = c.get("/place").text
    assert "Place · service cards" in t
    assert "birefnet_cutout" in t and "svg_writer_llm" in t
    assert "minor-assistant (owner, mem 210)" in t
    assert 'class="pl-pref high"' in t and 'class="pl-pref med"' in t
    # a dead door renders its reason, the page stays honest
    set_raise(RuntimeError("door down"))
    monkeypatch.setattr(meshplace, "_cache", (0.0, {}))
    t2 = c.get("/place").text
    assert "door down" in t2
