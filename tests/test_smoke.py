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
    assert r.json()["ok"] is True


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
