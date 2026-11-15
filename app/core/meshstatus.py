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

import datetime as _dt
import email.utils
import json
import os
import pathlib
import ssl as _ssl
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


def _leaf() -> dict:
    """This node's own hub-leaf expiry. 24h expiry IS the revocation lane,
    so the page counts it down: a dead re-enroll timer becomes a visible
    number (amber) before the mesh lanes go dark with 401s. Degrades with
    the reason -- a portal without a mesh identity is a normal state.
    """
    d = _meshdir()
    try:
        cfg = json.loads((d / "config.json").read_text())
        cert = (d / str(cfg.get("cert", "cert.crt"))).expanduser()
        not_after = _decode_not_after(cert)
    except Exception as e:  # noqa: BLE001 -- lane shows its own truth
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    hours = (not_after - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600.0
    return {"ok": True, "hours": round(hours, 1),
            "notAfter": not_after.astimezone().strftime("%Y-%m-%d %H:%M %Z")}


def _decode_not_after(path: pathlib.Path) -> _dt.datetime:
    # stdlib's private PEM reader (what ssl uses for its own cert dumps):
    # no dependency, parses exactly the two fields this lane needs.
    d = _ssl._ssl._test_decode_cert(str(path))
    return email.utils.parsedate_to_datetime(d["notAfter"])


def _meshdir() -> pathlib.Path:
    env = (os.environ.get("MESH_DIR") or "").strip()
    return pathlib.Path(env).expanduser() if env else pathlib.Path.home() / ".mesh"


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
        "leaf": _leaf(),
        "router": _router(),
        "forge": fr,
        "forge_router": fr_router,
        "token_src": fr_router.get("token_src", "?"),
    }
    _cache = (time.time(), out)
    return out
