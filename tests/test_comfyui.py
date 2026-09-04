"""Phase E (ComfyUI) tests — env-file service generation, launch config,
model inventory, and portal-side download placement.

All offline and hermetic: a throwaway AI_PORTAL_HOME, and a local HTTP server
for the download test. The systemd daemon-reload in serviceenv is stubbed so
no real user session is touched.
"""
import http.server
import json
import os
import shutil
import socketserver
import tempfile
import threading
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Isolation (same contract as test_updates.py): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-comfy-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

from app.core import serviceenv          # noqa: E402
from app.main import app                 # noqa: E402
from app.providers.comfyui import ComfyUI  # noqa: E402

C = ComfyUI()


def test_ensure_service_writes_env_wrapper_unit(monkeypatch):
    """Static unit + generated env file + exec wrapper, all under the
    (throwaway) home; env keeps multi-word args intact through quoting."""
    monkeypatch.setattr(serviceenv.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0,
                                                       "stdout": "", "stderr": ""})())
    C.ensure_service(log=lambda m: None)

    env = serviceenv.env_path("comfyui").read_text()
    assert 'COMFY_ARGS="--listen 127.0.0.1 --port 8188"' in env
    assert "COMFY_PYTHON=" in env and "COMFY_MAIN=" in env

    wrapper = serviceenv.wrapper_path("comfyui")
    assert wrapper.stat().st_mode & 0o111, "wrapper must be executable"
    assert 'exec "$COMFY_PYTHON" "$COMFY_MAIN" $COMFY_ARGS' in wrapper.read_text()

    unit = serviceenv.unit_path("ai-comfyui").read_text()
    assert f"EnvironmentFile={serviceenv.env_path('comfyui')}" in unit
    assert f"ExecStart={wrapper}" in unit
    assert "WantedBy=default.target" in unit


def test_config_defaults_validation_and_roundtrip():
    assert C.config() == {"listen": "127.0.0.1", "port": 8188, "extra_args": ""}

    with pytest.raises(ValueError):
        C.validate_config({"port": "abc"})
    with pytest.raises(ValueError):
        C.validate_config({"port": 0})
    with pytest.raises(ValueError):
        C.validate_config({"port": 70000})

    cfg = C.set_config({"listen": "127.0.0.1", "port": 8200,
                        "extra_args": "  --fast   --disable-auto-launch "})
    assert cfg == {"listen": "127.0.0.1", "port": 8200,
                   "extra_args": "--fast --disable-auto-launch"}
    assert C.config() == cfg  # persisted

    env = serviceenv.env_path("comfyui").read_text()
    assert '--port 8200 --fast --disable-auto-launch' in env


def test_inventory_groups_by_category():
    models = serviceenv.paths.COMFY_MODELS
    (models / "checkpoints").mkdir(parents=True, exist_ok=True)
    (models / "loras").mkdir(parents=True, exist_ok=True)      # empty -> omitted
    (models / ".hidden").mkdir(parents=True, exist_ok=True)    # skipped
    (models / "vae").mkdir(parents=True, exist_ok=True)
    (models / "checkpoints" / "sd_xl.safetensors").write_bytes(b"x" * 100)
    (models / "vae" / "a.safetensors").write_bytes(b"y" * 10)
    (models / "vae" / "b.safetensors").write_bytes(b"z" * 20)
    (models / ".hidden" / "junk").write_bytes(b"j")

    inv = {c["category"]: c for c in C.inventory()}
    assert set(inv) == {"checkpoints", "vae"}
    assert inv["checkpoints"]["count"] == 1
    assert inv["checkpoints"]["size"] == 100
    assert inv["vae"]["count"] == 2
    assert inv["vae"]["size"] == 30
    assert [f["name"] for f in inv["vae"]["files"]] == ["a.safetensors", "b.safetensors"]


def test_download_target_validation():
    models = serviceenv.paths.COMFY_MODELS
    (models / "loras").mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="unknown model category"):
        C.download_target("https://x/y.safetensors", "nope", None)
    with pytest.raises(ValueError, match="invalid model category"):
        C.download_target("https://x/y.safetensors", "a/b", None)
    with pytest.raises(ValueError, match="invalid model category"):
        C.download_target("https://x/y.safetensors", "..", None)

    d, f = C.download_target("https://hf.co/r/resolve/main/cool-lora.safetensors?x=1",
                             "loras", None)
    assert f.name == "cool-lora.safetensors" and f.parent == models / "loras"

    (models / "loras" / "cool-lora.safetensors").write_bytes(b"1")
    with pytest.raises(ValueError, match="already exists"):
        C.download_target("https://hf.co/r/resolve/main/cool-lora.safetensors",
                          "loras", None)
    # explicit filename can still override the derived one
    d, f = C.download_target("https://hf.co/r/resolve/main/cool-lora.safetensors",
                             "loras", "other.safetensors")
    assert f.name == "other.safetensors"


def test_download_file_streams_to_part_then_renames():
    """A local HTTP server stands in for Hugging Face: the file must land
    byte-identical, with no .part left behind."""
    models = serviceenv.paths.COMFY_MODELS
    (models / "checkpoints").mkdir(parents=True, exist_ok=True)
    payload = os.urandom(300_000)
    src = tempfile.NamedTemporaryFile(delete=False, suffix=".safetensors")
    src.write(payload)
    src.close()
    srv_dir = Path(src.name).parent

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = (f"http://127.0.0.1:{httpd.server_address[1]}"
               f"/{src.name.rsplit('/', 1)[-1]}")
        d, f = C.download_target(url, "checkpoints", None)
        lines = []
        res = C.download_file(url, d, f, lines.append)
        assert (d / f.name).read_bytes() == payload
        assert res["bytes"] == len(payload)
        assert not list(d.glob("*.part"))
        assert any("placed" in ln for ln in lines)
    finally:
        httpd.shutdown()
        os.unlink(src.name)


def test_api_endpoints_shape():
    c = TestClient(app)
    assert c.post("/login", data={"password": "test-password-123"}).status_code in (200, 302)

    r = c.get("/api/services/comfyui/config")
    assert r.status_code == 200
    assert isinstance(r.json()["port"], int)

    r = c.post("/api/services/comfyui/config", json={"port": 9999})
    assert r.status_code == 200
    assert r.json()["config"]["port"] == 9999
    assert 'COMFY_ARGS="--listen 127.0.0.1 --port 9999' in \
        serviceenv.env_path("comfyui").read_text()

    r = c.post("/api/services/comfyui/config", json={"port": "nope"})
    assert r.status_code == 400

    r = c.get("/api/models/comfyui")
    assert r.status_code == 200
    cats = [x["category"] for x in r.json()["categories"]]
    assert "checkpoints" in cats

    r = c.post("/api/models/comfyui/download",
               json={"url": "https://x/y.safetensors", "category": "nope"})
    assert r.status_code == 400
