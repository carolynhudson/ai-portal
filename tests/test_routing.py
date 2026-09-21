"""Route-ordering regressions.

FastAPI answers a request with the *first* matching route, and the generic
service control endpoint ``/api/services/{key}/{action}`` matches any two
segments — so a specific route registered after it is silently shadowed, and
because the generic route requires a JSON body the caller gets a confusing 422
about a missing body instead of the endpoint they called. That is exactly what
happened to ``/api/services/llamacpp/ensure``; these tests keep it from coming
back.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-route-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import paths  # noqa: E402
from app.main import app  # noqa: E402

GENERIC = "/api/services/{key}/{action}"


@pytest.fixture()
def router_launchable():
    """Seed the one thing /ensure verifies: an executable at the launcher path.

    This test used to pass only because tests/test_stray_builds.py had already symlinked
    ~/ai/bin/llama-server into the same throwaway home. Run test_routing.py on its own and it
    fails with "no executable at ... (deploy a release first)". A route test that depends on
    another file's fixtures is a latent flake: renaming or reordering that file breaks this
    one with an error that points at neither.
    """
    paths.AI_BIN.mkdir(parents=True, exist_ok=True)
    exe = paths.AI_BIN / "llama-server"
    exe.write_text("#!/bin/sh\necho 'b99999'\n")
    exe.chmod(0o755)
    return exe


def _order() -> list[str]:
    return [getattr(r, "path", "") for r in app.routes]


def test_specific_service_routes_are_registered_before_the_generic_matcher():
    order = _order()
    generic = order.index(GENERIC)
    specific = [
        "/api/services",
        "/api/services/comfyui/ensure",
        "/api/services/llamacpp/ensure",
        "/api/services/comfyui/config",
    ]
    for path in specific:
        assert path in order, f"{path} is not registered"
        assert order.index(path) < generic, (
            f"{path} would be swallowed by {GENERIC} — register specific routes "
            f"before the generic service-action endpoint")


def test_specific_settings_routes_are_not_templates_only():
    order = _order()
    for path in ("/api/settings/services/{service}",
                 "/api/settings/services/{service}/bind",
                 "/api/settings/services/{service}/validate",
                 "/api/settings/llamacpp/apikey"):
        assert path in order


def test_ensure_endpoint_answers_rather_than_asking_for_a_body(router_launchable):
    """The shadowing bug surfaced as a 422 'Field required: body'.

    `verify.ok is True` is the assertion that matters here: it is what proves the specific
    route answered rather than the generic matcher. So the launcher executable has to exist,
    which is what the fixture provides.
    """
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    r = c.post("/api/services/llamacpp/ensure")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "verify" in body and body["verify"]["ok"] is True
    assert body["wrapper"].endswith("ai-llamacpp.sh")
    # and it is idempotent
    assert c.post("/api/services/llamacpp/ensure").status_code == 200


def test_generic_service_action_still_rejects_nonsense():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    assert c.post("/api/services/llamacpp/teleport",
                  json={"confirm": True}).status_code == 404
    assert c.post("/api/services/nope/start",
                  json={"confirm": True}).status_code == 404
    assert c.post("/api/services/llamacpp/stop", json={}).status_code == 400
