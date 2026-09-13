"""Phase A smoke tests — read-only, run against a throwaway AI_PORTAL_HOME.

Run:  uv run pytest -q
"""
import os
import tempfile

# Point the portal at a throwaway home so the test never touches ~/ai.
os.environ["AI_PORTAL_HOME"] = tempfile.mkdtemp(prefix="aipp-test-")
os.environ["AI_PORTAL_HOST"] = "127.0.0.1"
os.environ["AI_PORTAL_PORT"] = "18090"
os.environ["AI_PORTAL_PASSWORD"] = "test-password-123"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def test_healthz_is_open():
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # identity: "deployed" and "serving" must be comparable in one response
    assert body["app_version"]
    assert "build" in body and "sha" in body["build"]
    assert body["pid"] > 0 and body["uptime_s"] >= 0


def test_pages_require_auth():
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    r = c.get("/api/status", follow_redirects=False)
    assert r.status_code == 401


def test_bad_password_rejected():
    c = TestClient(app)
    r = c.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401


def test_login_then_status_and_models():
    c = TestClient(app)
    r = c.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.cookies.get("aipp")

    st = c.get("/api/status")
    assert st.status_code == 200
    body = st.json()
    assert set(body["providers"]) == {"llamacpp", "comfyui", "huggingface"}
    assert "llama_local" in body["health"]

    models = c.get("/api/models")
    assert models.status_code == 200
    # temp home has no models dir -> empty lists, but well-formed
    assert set(models.json()) == {"live", "disk"}

    m = c.get("/api/metrics")
    assert m.status_code == 200
    assert "memory" in m.json() and "disk" in m.json()


def test_pages_render():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    for path in ("/", "/models", "/metrics"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "<html" in r.text.lower()


def test_router_endpoint():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.get("/api/router")
    assert r.status_code == 200
    body = r.json()
    # Every key the dashboard reads must be present, whether the router is up or down.
    assert set(body) >= {"host", "service", "web", "api", "loaded", "tps", "activity"}
    assert "state" in body["service"]
    assert isinstance(body["activity"], list)


def test_dashboard_renders_router_hero():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.get("/")
    assert r.status_code == 200
    # The live router hero + its live-update wiring are present in the page.
    assert "router-hero" in r.text
    assert "router-feed" in r.text
    assert "/api/router" in r.text


def test_parse_filters_proxy_spam():
    """The router logs one proxy_request line per forwarded request (our own
    /metrics poll included); those must not crowd out real events."""
    from app import router as rtr

    lines = [
        "srv: proxy_request: proxying request to model Qwen3.8-27B-UD-Q8_K_XL on port 55329",
        "slot release: id 0 | task 0 | stop processing: n_tokens = 8303, truncated = 0",
    ]
    out = rtr._parse(lines)
    assert len(out["events"]) == 1
    assert "proxying" not in out["events"][0]["text"]


def test_parse_extracts_tps():
    """b10641 prints per-decode speed as 'tg = 14.57 t/s' (column aligned)."""
    from app import router as rtr

    out = rtr._parse(["slot print_timing: id 3 | task 0 | n_gen = 4419, tg =  14.57 t/s, tg_3s =  12.92 t/s"])
    assert out["tps_latest"] == 14.57


def test_parse_extracts_completion():
    """Per-completion stats from the b10641 print_timing block, keyed by slot
    so interleaved slots don't corrupt each other (also proves the
    'prompt eval time' line is not misread as the generation 'eval time')."""
    from app import router as rtr

    lines = [
        "slot print_timing: id  3 | task 0 | prompt eval time =    1531.76 ms /   412 tokens (    3.72 ms per token,   268.97 tokens per second)",
        "slot print_timing: id  3 | task 0 |        eval time =  522281.04 ms /  7890 tokens (   66.20 ms per token,    15.10 tokens per second)",
        "slot print_timing: id  7 | task 1 | prompt eval time =      120.00 ms /    300 tokens (    0.40 ms per token,  2500.00 tokens per second)",
        "slot print_timing: id  3 | task 0 |       total time =  523812.80 ms /  8302 tokens",
        "slot print_timing: id  3 | task 0 | draft acceptance = 0.50945 ( 4770 accepted /  9363 generated), mean len =  2.53",
        "slot      release: id  3 | task 0 | stop processing: n_tokens = 8303, truncated = 0",
    ]
    out = rtr._parse(lines)
    assert len(out["completions"]) == 1
    c = out["completions"][0]
    assert (c["slot"], c["task"]) == (3, 0)
    assert c["prompt_tokens"] == 412 and c["gen_tokens"] == 7890
    assert c["prompt_tps"] == 268.97 and c["gen_tps"] == 15.1
    assert c["total_ms"] == 523812.8
    assert c["draft"]["rate"] == 0.50945 and c["draft"]["mean_len"] == 2.53


def test_service_memory_shape():
    """Best-effort cgroup read: None (router stopped) or positive ints."""
    from app.core import systemd

    m = systemd.service_memory("ai-llama-router")
    assert m is None or (isinstance(m["current"], int) and m["current"] > 0)


def test_services_endpoint_shape():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.get("/api/services")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"llamacpp", "comfyui", "portal"}
    for v in body.values():
        assert {"unit", "name", "state", "controllable"} <= set(v)


def test_service_action_validation():
    """Validation paths only — none of these may reach a real unit."""
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    # stop without confirm -> 400 (no systemctl call made)
    assert c.post("/api/services/llamacpp/stop", json={}).status_code == 400
    # unknown service / action -> 404
    assert c.post("/api/services/nope/start", json={"confirm": True}).status_code == 404
    assert c.post("/api/services/llamacpp/explode", json={"confirm": True}).status_code == 404
