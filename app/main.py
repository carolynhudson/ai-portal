"""FastAPI application: auth, HTML pages (HTMX+Jinja), and the JSON API.

Frontend note (decision D1): the UI is HTMX + Jinja with *no JS build step*.
Every read is also exposed as a ``/api/*`` JSON endpoint, and templates may
embed small inline ``<script>`` blocks, so adding a chart (e.g. CPU/RAM) is
just a ``<canvas>`` + a few lines of JS that ``fetch()`` an existing endpoint —
no front-end re-engineering.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import metrics, router
from .core import config as cfg
from .core import db, paths, systemd
from .providers import PROVIDERS, get as get_provider

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"


class LoginBody(BaseModel):
    password: str


class ServiceActionBody(BaseModel):
    confirm: bool = False

_status_cache: tuple[float, dict] = (0.0, {})
_STATUS_TTL = 10.0
_behind_cache: tuple[float, dict] = (0.0, {})
_BEHIND_TTL = 300.0


def _settings() -> cfg.Settings:
    # Lazy init: DB schema + auth (first run generates the password).
    global _settings_cache
    if _settings_cache is None:
        db.init()
        _settings_cache = cfg.load()
    return _settings_cache


_settings_cache: cfg.Settings | None = None


def _human(n: int | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _require_auth(request: Request) -> None:
    s = _settings()
    token = request.cookies.get(cfg.COOKIE_NAME)
    if token and cfg.token_valid(s, token):
        return
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="unauthenticated")
    raise HTTPException(status_code=302, headers={"Location": "/login"})


def _llama_behind() -> dict:
    global _behind_cache
    now = time.time()
    if now - _behind_cache[0] < _BEHIND_TTL:
        return _behind_cache[1]
    p = get_provider("llamacpp")
    res = p.behind() if p else {}
    _behind_cache = (now, res)
    return res


def create_app() -> FastAPI:
    app = FastAPI(title="AI Portal", version="0.1.0")
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    # ------------------------------------------------------------------ open
    @app.get("/healthz")
    def healthz():
        _settings()
        return {"ok": True, "portal": "up", "ts": time.time()}

    # ------------------------------------------------------------------ auth
    @app.get("/login")
    def login_get(request: Request, error: str | None = None):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": error})

    @app.post("/login")
    def login_post(request: Request, password: str = Form(...)):
        s = _settings()
        if s.password_hash and cfg.verify_password(password, s.password_hash):
            db.event("portal", "auth", "password login ok")
            resp = RedirectResponse(url="/", status_code=302)
            resp.set_cookie(cfg.COOKIE_NAME, cfg.make_token(s), httponly=True,
                            samesite="lax", path="/", max_age=cfg.SESSION_TTL)
            return resp
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "wrong password"},
            status_code=401)

    @app.post("/api/login")
    def api_login(body: LoginBody):
        s = _settings()
        if s.password_hash and cfg.verify_password(body.password, s.password_hash):
            db.event("portal", "auth", "api login ok")
            resp = JSONResponse({"ok": True})
            resp.set_cookie(cfg.COOKIE_NAME, cfg.make_token(s), httponly=True,
                            samesite="lax", path="/", max_age=cfg.SESSION_TTL)
            return resp
        raise HTTPException(status_code=401, detail="wrong password")

    @app.post("/logout")
    def logout():
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie(cfg.COOKIE_NAME, path="/")
        return resp

    # ------------------------------------------------------------- HTML pages
    def _provider_card(s) -> dict:
        rows = []
        if s.installed:
            rows.append(["source/lib", s.installed])
        if s.deployed:
            rows.append(["deployed", s.deployed])
        rows.append(["service", f"{s.service_name or '—'}: {s.service_state or '—'}"])
        d = s.detail
        if s.key == "llamacpp":
            if d.get("current_release"):
                rows.append(["release", d["current_release"]])
            if d.get("vulkan_sdk"):
                rows.append(["vulkan sdk", d["vulkan_sdk"].replace(str(paths.HOME), "~")])
        if s.key == "comfyui":
            v = d.get("venv", {})
            if v.get("python"):
                rows.append(["venv", f"py {v['python']} · torch {v.get('torch', '?')}"])
        if s.key == "huggingface":
            rows.append(["cache", _human(d.get("cache_size"))])
            if d.get("repos"):
                rows.append(["repos", ", ".join(r.replace("models--", "").replace("--", "/")
                                                 for r in d["repos"])])
        return {"name": s.name, "healthy": s.healthy,
                "pill": "ok" if s.healthy else (s.service_state or "n/a"),
                "rows": rows}

    @app.get("/")
    def index(request: Request, _=Depends(_require_auth)):
        s = _settings()
        providers = {p.key: _provider_card(p.status()) for p in PROVIDERS}
        health = _health_bundle()
        snap = router.snapshot()
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"providers": providers, "health": health,
                                           "r": snap, "settings": db.current_settings()})

    @app.get("/models")
    def models(request: Request, _=Depends(_require_auth)):
        p = get_provider("llamacpp")
        live = p.models_live() if p else []
        disk = p.models_scan() if p else []
        for m in disk:
            m["size_h"] = _human(m.get("size"))
        return templates.TemplateResponse(request, "models.html",
                                          {"live": live, "disk": disk,
                                           "diskroot": str(paths.LLAMA_MODELS)})

    @app.get("/metrics")
    def metrics_page(request: Request, _=Depends(_require_auth)):
        snap = metrics.snapshot()
        disk = [[k, f"{_human(v['used'])} / {_human(v['total'])} ({v['percent']:.0f}%)"]
                for k, v in (snap["disk"] or {}).items() if v]
        gpu = snap.get("gpu")
        gpu_txt = ("GPU {use_percent}%".format(use_percent=gpu["use_percent"])
                   if gpu and gpu.get("use_percent") is not None else "n/a (rocm-smi)")
        return templates.TemplateResponse(request, "metrics.html",
                                          {"disk": disk, "gpu": gpu_txt,
                                           "settings": db.current_settings()})

    # ----------------------------------------------------------------- JSON
    def _health_bundle() -> dict:
        s = _settings()
        rp = systemd.reboot_pending()
        behind = _llama_behind()
        disk = metrics.disk()
        bundle = {
            "reboot_required": rp.get("reboot_required", False),
            "vulkan_sdk": str(paths.VULKAN_SDK).replace(str(paths.HOME), "~"),
            "llama_local": behind.get("local", "—"),
            "llama_origin": behind.get("origin", "—") or "—",
            "llama_behind": behind.get("behind", "—"),
            "ai_disk": f"{_human(disk.get('ai', {}).get('used'))} / {_human(disk.get('ai', {}).get('total'))}" if disk.get("ai") else "—",
            "root_disk": f"{_human(disk.get('root', {}).get('used'))} / {_human(disk.get('root', {}).get('total'))}" if disk.get("root") else "—",
        }
        return bundle

    @app.get("/api/status")
    def api_status(_=Depends(_require_auth)):
        global _status_cache
        now = time.time()
        if now - _status_cache[0] < _STATUS_TTL:
            return _status_cache[1]
        providers = {p.key: p.status().to_dict() for p in PROVIDERS}
        body = {"providers": providers, "health": _health_bundle(), "ts": now}
        # persist a snapshot for later history use
        for p in PROVIDERS:
            st = providers[p.key]
            db.upsert_tool({
                "key": p.key, "name": st["name"], "kind": st["kind"],
                "installed": st["installed"], "deployed": st["deployed"],
                "service_name": st["service_name"], "service_state": st["service_state"],
                "healthy": 1 if st["healthy"] else 0, "detail": str(st["detail"])[:2000],
                "updated_at": now,
            })
        _status_cache = (now, body)
        return body

    @app.get("/api/providers/{key}")
    def api_provider(key: str, _=Depends(_require_auth)):
        p = get_provider(key)
        if not p:
            raise HTTPException(status_code=404, detail="unknown provider")
        return p.status().to_dict()

    @app.get("/api/models")
    def api_models(_=Depends(_require_auth)):
        p = get_provider("llamacpp")
        return {"live": p.models_live() if p else [],
                "disk": p.models_scan() if p else []}

    @app.get("/api/metrics")
    def api_metrics(_=Depends(_require_auth)):
        return metrics.snapshot()

    @app.get("/api/health")
    def api_health(_=Depends(_require_auth)):
        return _health_bundle()

    @app.get("/api/router")
    def api_router(_=Depends(_require_auth)):
        """Live router state (web UI, REST API, models, loaded, tokens/s, log tail)."""
        return router.snapshot()

    @app.get("/api/settings")
    def api_settings(_=Depends(_require_auth)):
        """Tunable settings (poll rates, feature flags) for the dashboard."""
        return db.current_settings()

    # ------------------------------------------------- service controls (B)
    @app.get("/api/services")
    def api_services(_=Depends(_require_auth)):
        """Managed user units and their live state."""
        out = {}
        for key, meta in systemd.MANAGED_UNITS.items():
            st = systemd.unit_state(meta["unit"])
            out[key] = {
                "unit": meta["unit"], "name": meta["name"],
                "state": st.get("ActiveState", "unknown"),
                "sub": st.get("SubState"),
                "controllable": st.get("LoadState", "not-found") != "not-found",
                "confirm": meta.get("confirm"),
            }
        return out

    @app.post("/api/services/{key}/{action}")
    def api_service_action(key: str, action: str, body: ServiceActionBody,
                           _=Depends(_require_auth)):
        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=404, detail="unknown action")
        if key not in systemd.MANAGED_UNITS:
            raise HTTPException(status_code=404, detail="unknown service")
        if action in ("stop", "restart") and not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        res = systemd.service_action(key, action)
        if not res.get("ok"):
            raise HTTPException(status_code=500,
                                detail=res.get("error") or "action failed")
        db.event("portal", "service",
                 f"{action} {key} ({systemd.MANAGED_UNITS[key]['unit']})")
        return res

    return app


app = create_app()


def run() -> None:
    """Console entrypoint (pyproject [project.scripts] ai-portal)."""
    import uvicorn
    s = _settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    # Run from the repo root:  python -m app.main
    # (Not `python app/main.py` — that has no package context for relative imports.)
    run()
