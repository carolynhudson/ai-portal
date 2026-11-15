"""mesh.status -- the P2 read-only aggregation (docs/MESH_HARNESS_PLAN.md).

One honest view of the mesh as it is right now: the hub's node registry
(enumeration, via app.core.meshhub), this box's llama.cpp router lane,
the remote ComfyUI forge, and the forge's llama router (probed with the
ONE-token fetched FROM THE HUB, falling back to the file -- the
cutover path the forge enrollment task follows).

Read-only by construction: no verbs here; mutations belong to their
own gated surfaces. Every lane degrades to {"ok": false, "error": ...};
the page must render the mesh with parts of it down.
"""
from __future__ import annotations

import os
import pathlib
import time

import httpx

from . import meshhub, portalsettings, serviceconf

# forge router = the forge's llama.cpp endpoint; the settings URL is the
# ComfyUI base, and the router conventionally lives two ports up
# (8188 -> 8082, the mesh's port ledger). Derived, overridable by env.
_FORGE_ROUTER_ENV = "AIPP_FORGE_ROUTER_URL"
_TOK_FILE = pathlib.Path.home() / "ai/config/subagent_key.txt"
_TTL = 20.0
_cache: tuple[float, dict] = (0.0, {})


def _forge_base() -> str:
    return str(portalsettings.get_value("remote_forge_url") or "").rstrip("/")


def _forge_router_url() -> str:
    env = (os.environ.get(_FORGE_ROUTER_ENV) or "").strip()
    if env:
        return env.rstrip("/")
    base = _forge_base()
    if "8188" in base:
        return base.replace("8188", "8082")
    return ""


def _token() -> tuple[str, str]:
    """The ONE-token, hub first, file fallback. Returns (token, source);
    the source is shown on the page -- the cutover made visible."""
    try:
        return meshhub.get_text("subagent-key").strip(), "hub"
    except Exception:  # noqa: BLE001 -- the fallback IS the point
        pass
    try:
        return _TOK_FILE.read_text().strip(), "file"
    except OSError:
        return "", "none"


def _router() -> dict:
    from ..providers.llamacpp import _router_models
    ms = _router_models()
    if ms and str(ms[0].get("id", "")).startswith("(router unreachable"):
        return {"ok": False, "error": str(ms[0].get("id"))}
    return {"ok": True, "models": [m.get("id", "?") for m in ms]}


def _forge() -> dict:
    url = _forge_base()
    if not url:
        return {"ok": False, "error": "remote_forge_url unset"}
    try:
        r = httpx.get(f"{url}/system_stats", timeout=3.0)
        if r.status_code != 200:
            return {"ok": False, "url": url, "error": f"HTTP {r.status_code}"}
        ver = (r.json().get("system") or {}).get("comfyui_version")
        return {"ok": True, "url": url, "version": ver}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": f"{type(e).__name__}"}


def _forge_router(tok_src: str) -> dict:
    url = _forge_router_url()
    if not url:
        return {"ok": False, "error": "forge router URL not derivable (set AIPP_FORGE_ROUTER_URL)"}
    tok, src = _token() if tok_src == "auto" else (tok_src, tok_src)
    hdr = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = httpx.get(f"{url}/v1/models", timeout=4.0, headers=hdr)
        if r.status_code != 200:
            return {"ok": False, "url": url, "error": f"HTTP {r.status_code} (token: {src})",
                    "token_src": src}
        ids = [m.get("id", "?") for m in r.json().get("data", [])]
        return {"ok": True, "url": url, "models": ids, "token_src": src}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": f"{type(e).__name__}", "token_src": src}


def bundle(force: bool = False) -> dict:
    """The aggregation. Cached for _TTL seconds (a view that polls the hub
    per request starts owning the hub's hot path)."""
    global _cache
    if not force and time.time() - _cache[0] < _TTL and _cache[1]:
        return _cache[1]
    hub: dict
    try:
        nodes = meshhub.nodes()
        for n in nodes:
            age = time.time() - (n.get("last") or 0)
            n["ago"] = (f"{int(age)}s" if age < 120 else
                        f"{int(age // 60)}m" if age < 7200 else f"{age / 3600:.1f}h")
        hub = {"ok": True, "nodes": nodes}
    except meshhub.MeshHubError as e:
        hub = {"ok": False, "error": str(e)[:160]}
    fr = _forge()
    fr_router = _forge_router("auto")
    out = {
        "at": time.time(),
        "hub": hub,
        "router": _router(),
        "forge": fr,
        "forge_router": fr_router,
        "token_src": fr_router.get("token_src", "?"),
    }
    _cache = (time.time(), out)
    return out
