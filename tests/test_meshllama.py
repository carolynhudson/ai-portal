"""mesh_lease client (portal as placement actor): admission-first flow,
honest refusals, the two-phase confirm gate, and the /place surfaces.

No live doors (tests-never-touch-live-infra): meshplace.mcp_tool and the
token lane are injected. The LAW under test: a verb runs only after a
grant AND the second, nonce-backed click; failures un-admit; the local
(halo/loopback) door is never an offload target.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-lease-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ["MESH_DIR"] = tempfile.mkdtemp(prefix="aipp-lease-mesh-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import meshllama, meshplace, meshstatus  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    os.environ["MESH_DIR"] = tempfile.mkdtemp(prefix="aipp-lease-mesh-")
    meshllama._gated.clear()


@pytest.fixture()
def doors(monkeypatch):
    monkeypatch.setattr(meshstatus, "_token", lambda: ("tok", "hub"))
    monkeypatch.setattr(meshplace, "_doors",
                        lambda: ["http://127.0.0.1:8092", "http://forge:8092"])

    def inject(calls):
        """calls: {tool: result|Exception}; records (url, tool, args)."""
        seen = []

        def fake(url, tool, args, tok):
            seen.append((url, tool, args))
            if tool == "node_caps":
                return {"node": "rainbowblock"}
            v = calls.get(tool, {"decision": "denied", "reason": "unconfigured"})
            if isinstance(v, Exception):
                raise v
            return v
        monkeypatch.setattr(meshplace, "mcp_tool", fake)
        return seen
    return inject


def test_offload_admission_then_apply(doors):
    seen = doors({
        "mesh_lease": {"decision": "granted",
                       "lease": {"id": "L1", "vram_mib": 3072, "ttl_s": 14400,
                                 "granted_unix": 1, "expires_unix": 9e18}},
        "mesh_router_offload": {"node": "rainbowblock", "lease_id": "L1",
                                "forge": {"ok": True, "warm_ms": 4200}},
    })
    r = meshllama.offload("rainbowblock", "flint", 3072)
    assert r["ok"] and r["state"] == "offloaded" and r["warm_ms"] == 4200
    tools = [t for (_u, t, _a) in seen]
    assert tools[:2] == ["node_caps", "mesh_lease"]       # admission FIRST
    assert tools[2] == "mesh_router_offload"
    ap = [a for (_u, t, a) in seen if t == "mesh_router_offload"][0]
    assert ap["ngl"] == 99 and ap["action"] == "apply" and ap["lease_id"] == "L1"
    t2 = meshllama.tracked()
    assert t2 and t2[0]["model"] == "flint" and not t2[0]["lease_expired"]


def test_denial_never_applies(doors):
    seen = doors({"mesh_lease": {"decision": "denied", "reason": "vram 3072 > 1200",
                                 "free_vram_mib": 1200}})
    r = meshllama.offload("rainbowblock", "flint", 3072)
    assert r["ok"] is False and r["state"] == "denied" and "1200" in r["reason"]
    assert [t for (_u, t, _a) in seen] == ["node_caps", "mesh_lease"]  # no apply
    assert meshllama.tracked() == []


def test_apply_failure_unadmits(doors):
    seen = doors({
        "mesh_lease": {"decision": "granted",
                       "lease": {"id": "L2", "vram_mib": 2048, "ttl_s": 600}},
        "mesh_router_offload": RuntimeError("forge-mcp offline"),
    })
    r = meshllama.offload("rainbowblock", "flint", 2048)
    assert r["ok"] is False and r["state"] == "apply-failed" and r["lease_released"]
    assert [t for (_u, t, _a) in seen][-1] == "lease_release"
    assert meshllama.tracked() == []


def test_loopback_never_a_target(doors):
    meshplace_doors = meshplace._doors()
    assert "http://127.0.0.1:8092" in meshplace_doors
    with pytest.raises(RuntimeError, match="loopback doors are never"):
        meshllama.offload("halo", "swift", 2048)


def test_release_reverts_then_frees(doors):
    doors({
        "mesh_lease": {"decision": "granted",
                       "lease": {"id": "L3", "vram_mib": 2048, "ttl_s": 600}},
        "mesh_router_offload": {"forge": {"ok": True, "note": "idle-sleep 900 s"}},
        "lease_release": {"decision": "released"},
    })
    meshllama.offload("rainbowblock", "flint", 2048)
    r = meshllama.release("rainbowblock", "flint")
    assert r["ok"] and r["state"] == "reverted" and r["lease"] == "released"
    assert meshllama.tracked() == []


def test_gate_single_use_and_expiry():
    n = meshllama.gate("rainbowblock", "flint", "offload", 3072, 8192)
    assert meshllama.take(n)["model"] == "flint"
    assert meshllama.take(n) is None                       # single use
    n2 = meshllama.gate("rainbowblock", "flint", "offload", 3072, 8192)
    meshllama._gated[n2]["at"] -= meshllama.GATE_TTL_S + 1
    assert meshllama.take(n2) is None                      # stale tab: no verb


def _login(c):
    c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
           follow_redirects=False)


def test_place_page_actor_surfaces(monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: {
        "at": 0, "token_src": "hub", "cards": 1, "leased": 0,
        "committed_vram_mib": 0, "policy": "minor-assistant nodes: forge, rainbowblock",
        "nodes": [{"ok": True, "url": "http://forge:8092", "node": "rainbowblock",
                   "policy": "minor-assistant (owner, mem 210)", "leases": {},
                   "cards": [{"service": "flint", "kind": "llm-router", "tier": "t",
                              "fills": ["prompt"], "requires": {"backend": "cpu",
                                                                "vram_gb_min": 3},
                              "limits": {}, "state": "catalog", "pref": "high"}]}]})
    c = TestClient(app)
    _login(c)
    t = c.get("/place").text
    assert "VRAM offload" in t                      # the law, stated
    assert 'value="flint"' in t and "Offload" in t  # the control exists
    # gate -> banner (no verb has run); apply -> honest refusal flash
    r = c.post("/place", data={"node": "rainbowblock", "model": "flint",
                               "op": "offload", "phase": "gate", "vram_mib": 3072},
               follow_redirects=False)
    assert r.status_code == 302 and "gate=" in r.headers["location"]
    t = c.get(r.headers["location"]).text
    assert "Confirm: offload · rainbowblock · flint" in t
    assert "3072 MiB" in t       # banner shows the EXACT ask that apply will run
    assert "Apply (confirmed)" in t
    # stale nonce cannot fire
    t = c.post("/place", data={"node": "rainbowblock", "model": "flint",
                               "op": "offload", "phase": "apply", "nonce": "deadbeef",
                               "vram_mib": 3072}, follow_redirects=False)
    assert "gate+expired" in t.headers["location"]


def test_place_page_held_offloads_panel(monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: {
        "at": 0, "token_src": "hub", "cards": 0, "leased": 0,
        "committed_vram_mib": 0, "policy": "p", "nodes": []})
    meshllama._record("rainbowblock", "flint",
                      {"id": "L9", "vram_mib": 3072, "ttl_s": 600,
                       "granted_unix": 1, "expires_unix": 1 + 3600})
    c = TestClient(app)
    _login(c)
    t = c.get("/place").text
    assert "VRAM offloads held by this portal" in t
    assert "L9" in t and "Release (revert -ngl 0)" in t
