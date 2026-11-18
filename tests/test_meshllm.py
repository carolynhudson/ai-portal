"""meshllm: the /llm gateway -- resolution, failover, auth lanes, headers.

The MCP transport is injected (the suite never touches a live door, and
the proxy sender is a parameter). The laws under test: READ PATH ONLY,
failover strictly BEFORE the first byte, placement visible per request,
loopback routers on remote doors rewritten to the door host.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-llm-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ.setdefault("AIPP_LLM_GATEWAY_TOKEN", "gateway-test-token-0123456789")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import meshllm, meshplace  # noqa: E402
from app.main import app  # noqa: E402


def _card(node, service, state="unloaded", ctx=262144, gib=29.0):
    return {"node": node, "kind": "llm-router", "service": service,
            "state": state, "limits": {"context": ctx},
            "requires": {"ram_gb_min": 34, "backend": "llama-cuda-cpu"},
            "measured": {"weights_bytes": int(gib * 2**30), "kv_bytes": 2**30,
                         "gguf": {"block_count": 64, "embedding_length": 5120,
                                  "attention_count": 40, "attention_kv_count": 8,
                                  "rope_dimension_count": 128,
                                  "feed_forward_length": 27520,
                                  "vocab_size": 151936, "filetype": "Q4_K_M"}}}


BUNDLE = {"nodes": [
    {"ok": True, "url": "http://127.0.0.1:8092", "node": "halo",
     "router_url": "http://127.0.0.1:8080",
     "cards": [_card("halo", "ThinkingCap-Qwen3.8-27B-Q4_K_M")]},
    {"ok": True, "url": "http://192.168.1.25:8092", "node": "rainbowblock",
     "router_url": "http://127.0.0.1:8082",
     "cards": [_card("rainbowblock", "ThinkingCap-Qwen3.8-27B-Q4_K_M",
                     state="loaded")]},
]}


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_gateway_token_env_override_is_the_test_lane():
    assert meshllm.gateway_token() == "gateway-test-token-0123456789"


def test_gateway_token_mints_once_at_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPP_LLM_GATEWAY_TOKEN", raising=False)
    from app.core import paths
    monkeypatch.setattr(paths, "PORTAL_STATE", tmp_path)
    t1 = meshllm.gateway_token()
    t2 = meshllm.gateway_token()
    assert t1 == t2 and len(t1) >= 32
    p = tmp_path / "llm_gateway_token.txt"
    assert (p.stat().st_mode & 0o077) == 0


def test_lan_router_rewrite():
    assert meshllm._lan_router("http://127.0.0.1:8082", "http://192.168.1.25:8092") \
        == "http://192.168.1.25:8082"
    assert meshllm._lan_router("http://127.0.0.1:8080", "http://127.0.0.1:8092") \
        == "http://127.0.0.1:8080"
    assert meshllm._lan_router("http://10.0.0.5:8082", "http://192.168.1.25:8092") \
        == "http://10.0.0.5:8082"


def test_resolve_prefers_the_loaded_exact_twin_and_rewrites_the_router():
    _req, ranked, _f = meshllm.resolve({"model": "ThinkingCap-Qwen3.8-27B-Q4_K_M"},
                                       BUNDLE)
    assert ranked[0]["node"] == "rainbowblock"
    assert ranked[0]["router_url"] == "http://192.168.1.25:8082"
    assert ranked[1]["router_url"] == "http://127.0.0.1:8080"


def test_chat_failover_is_pre_response_and_visible():
    calls = []

    def send(router_url, service, body, tok, timeout=300.0):
        calls.append(router_url)
        if router_url.startswith("http://192.168"):
            raise ConnectionError("door down")
        return FakeResp()

    r = meshllm.chat({"model": "ThinkingCap-Qwen3.8-27B-Q4_K_M"}, BUNDLE,
                     "tok", send=send)
    assert r["ok"] and r["placed"]["node"] == "halo"
    assert calls[0] == "http://192.168.1.25:8082"  # loaded twin tried first
    assert r["attempts"][0]["result"] == "ConnectionError"


def test_chat_all_down_reports_every_attempt():
    def send(router_url, service, body, tok, timeout=300.0):
        raise ConnectionError("everything is down")

    r = meshllm.chat({"model": "auto"}, BUNDLE, "", send=send)
    assert not r["ok"]
    assert len(r["attempts"]) == 2


def test_failover_request_derives_from_the_source_card():
    req = meshllm.failover_request(BUNDLE, "thinkingcap-qwen3.8-27b-q4_k_m")
    assert req["ctx"] == 262144           # the source ctx IS the floor
    assert req["min_params_ratio"] == 0.75
    assert req["prefer_loaded"] is True
    assert req["params_b"] and req["weights_bytes"]
    assert meshllm.failover_request(BUNDLE, "no-such-model") is None
    # the gateway can then proxy a failover ask end to end
    _req, ranked, _f = meshllm.resolve(dict(req, model="auto"), BUNDLE)
    assert ranked and ranked[0]["router_url"]


def test_models_lists_auto_always():
    rows = meshllm.models(BUNDLE)
    ids = {r["id"] for r in rows}
    assert "auto" in ids and "ThinkingCap-Qwen3.8-27B-Q4_K_M" in ids


# ------------------------------------------------------------- route lanes

def test_llm_routes_require_auth(client, monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: BUNDLE)
    r = client.get("/llm/v1/models")
    assert r.status_code == 401
    r = client.post("/llm/v1/chat/completions", json={"model": "auto"})
    assert r.status_code == 401


def test_llm_models_with_gateway_token(client, monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: BUNDLE)
    r = client.get("/llm/v1/models",
                   headers={"Authorization": "Bearer gateway-test-token-0123456789"})
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["data"]}
    assert "auto" in ids


def test_llm_chat_503_names_every_attempt(client, monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: BUNDLE)

    def send(router_url, service, body, tok, timeout=300.0):
        raise ConnectionError("refused")

    monkeypatch.setattr(meshllm, "_send", send)
    r = client.post("/llm/v1/chat/completions",
                    headers={"Authorization": "Bearer gateway-test-token-0123456789"},
                    json={"model": "ThinkingCap-Qwen3.8-27B-Q4_K_M"})
    assert r.status_code == 503
    det = r.json()["detail"]
    assert "attempts" in det and len(det["attempts"]) == 2


def test_llm_chat_success_carries_visible_placement(client, monkeypatch):
    monkeypatch.setattr(meshplace, "bundle", lambda force=False: BUNDLE)

    def send(router_url, service, body, tok, timeout=300.0):
        return FakeResp()

    monkeypatch.setattr(meshllm, "_send", send)
    r = client.post("/llm/v1/chat/completions",
                    headers={"Authorization": "Bearer gateway-test-token-0123456789"},
                    json={"model": "ThinkingCap-Qwen3.8-27B-Q4_K_M"})
    assert r.status_code == 200
    assert "rainbowblock" in r.headers["x-aipp-place"]
    import urllib.parse
    att = urllib.parse.unquote(r.headers["x-aipp-attempts"])
    assert "rainbowblock" in att and "200" in att
