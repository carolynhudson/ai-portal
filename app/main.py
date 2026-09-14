"""FastAPI application: auth, HTML pages (HTMX+Jinja), and the JSON API.

Frontend note (decision D1): the UI is HTMX + Jinja with *no JS build step*.
Every read is also exposed as a ``/api/*`` JSON endpoint, and templates may
embed small inline ``<script>`` blocks, so adding a chart (e.g. CPU/RAM) is
just a ``<canvas>`` + a few lines of JS that ``fetch()`` an existing endpoint —
no front-end re-engineering.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import httpx

from . import __version__, metrics, router as router_mod
from .core import config as cfg, llamakey, logsetup, network, ratelimit, serviceconf
from .core import serviceenv
from .core import db, modeladd, paths, systemd, updater
from .providers import PROVIDERS, get as get_provider

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
STARTED_AT = time.time()


def _build_info() -> dict:
    """Which commit this process is running.

    ``app.deploy`` stamps ``BUILD.json`` into the deployed tree; comparing that
    with ``/healthz`` is how "deployed" and "actually serving" become one
    checkable fact (an ``enable --now`` on an active unit does not restart it,
    which once made a deploy look successful while the old code kept running).
    A source checkout has no BUILD.json — which is itself the answer.
    """
    for p in (HERE.parent / "BUILD.json", HERE / "BUILD.json"):
        try:
            info = json.loads(p.read_text())
            if isinstance(info, dict):
                return info
        except (OSError, ValueError):
            continue
    return {"sha": None, "source": str(HERE.parent), "kind": "checkout"}


BUILD = _build_info()


class LoginBody(BaseModel):
    password: str


class ServiceActionBody(BaseModel):
    confirm: bool = False


class UpdateBody(BaseModel):
    target: str = "HEAD"    # "HEAD" (remote default branch) | tag | commit sha
    restart: bool = True    # restart the router once the new release is deployed
    smoke: bool = True      # boot a throwaway worker from the staged release
    dry: bool = False       # stop after staging+verify (deploy nothing)


class RollbackBody(BaseModel):
    release: str            # a name from /api/updates/llamacpp releases
    confirm: bool = False


class ComfyConfigBody(BaseModel):
    listen: str | None = None
    port: int | str | None = None   # str tolerated so we can 400 with a message
    extra_args: str | None = None


class BindBody(BaseModel):
    """Bind request for one managed service.

    ``bind`` is the mode: ``loopback`` (this machine), ``lan`` (one interface
    address — ``address`` optional, defaults to the default-route nic), or
    ``all`` (0.0.0.0 — every interface). ``external`` is the legacy boolean
    shorthand for ``all``/``loopback`` and still works.
    """
    bind: str | None = None
    address: str | None = None
    external: bool | None = None
    restart: bool = False       # restart the unit now (router: confirm first)
    confirm: bool = False


class ApiKeyBody(BaseModel):
    action: str = "generate"    # generate | revoke | rotate
    force: bool = False


class ComfyDownloadBody(BaseModel):
    url: str
    category: str
    filename: str | None = None


class ComfyUpdateBody(BaseModel):
    restart: bool = True


class ComfyRollbackBody(BaseModel):
    commit: str
    confirm: bool = False


class AddModelBody(BaseModel):
    """One of: url (direct file), path (local file), hf_cli (pasted
    huggingface-cli/hf download command), or hf_repo (org/name)."""
    url: str | None = None
    path: str | None = None
    hf_cli: str | None = None
    hf_repo: str | None = None
    filename: str | None = None
    category: str | None = None   # safetensors override; else auto-classified
    model_dir: str | None = None  # llama model dir (for mmproj/draft files)


class SearchModelsBody(BaseModel):
    """Search parameters for Hugging Face models."""
    query: str
    service: str = "llamacpp"  # "llamacpp" or "comfyui"
    model_type: str | None = None  # pipeline_tag or specific category
    limit: int = 20

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


def comfy_snapshot() -> dict:
    """Live ComfyUI state for the dashboard hero (mirrors router_mod.snapshot()).

    Probes the endpoint as configured (see :meth:`ComfyUI.endpoint`) and shows
    the bind, so the hero says the same thing the Settings page does about who
    can reach it.
    """
    p = get_provider("comfyui")
    cfg = p.config()
    st = systemd.unit_state(paths.COMFY_UNIT)
    base = p.endpoint()
    web = router_mod.probe(f"{base}/system_stats")
    version = None
    if web["ok"]:
        try:
            version = httpx.get(f"{base}/system_stats",
                                timeout=3).json()["system"]["comfyui_version"]
        except Exception:  # noqa: BLE001
            pass
    return {
        "host": base,
        "port": cfg["port"],
        "bind": serviceconf.service_ports()["comfyui"],
        "service": {"state": st.get("ActiveState", "unknown"),
                    "sub": st.get("SubState"),
                    "since": st.get("ActiveEnterTimestamp", "").split(" ")[0] or None},
        "web": web,
        "version": version,
        "memory": systemd.service_memory(paths.COMFY_UNIT),
        "activity": systemd.journal_tail(paths.COMFY_UNIT, 200)[-80:],
    }


def create_app() -> FastAPI:
    log = logsetup.setup()          # level: AI_PORTAL_LOG_LEVEL (env/unit)
    app = FastAPI(title="AI Portal", version="0.1.0")
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
    log.info("AI Portal %s: home=%s ai_root=%s state=%s",
             app.version, paths.HOME, paths.AI_ROOT, paths.PORTAL_STATE)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """Log the traceback with the request context, then hand back a 500.

        Without this an endpoint failure is a bare 500 in the browser and a
        truncated line in the journal; the file log needs the path + stack to
        be diagnosable after the fact.
        """
        log.exception("unhandled error on %s %s", request.method,
                      request.url.path)
        return JSONResponse(status_code=500,
                            content={"ok": False,
                                     "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------ open
    @app.get("/healthz")
    def healthz():
        """Liveness + identity: version, deployed sha, pid, uptime.

        Identity matters operationally — without the sha there is no cheap way
        to tell whether the code answering is the code you just deployed.
        """
        _settings()
        return {"ok": True, "portal": "up", "ts": time.time(),
                "app_version": __version__, "build": BUILD,
                "pid": os.getpid(), "uptime_s": round(time.time() - STARTED_AT, 1)}

    # ------------------------------------------------------------------ auth
    @app.get("/login")
    def login_get(request: Request, error: str | None = None):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": error})

    def _login_blocked(request: Request):
        """Throttle key + 429 payload when a source is out of attempts.

        Needed because the portal listens on the LAN (decision D2): the app
        password is the only gate, so failed attempts are counted per source.
        """
        key = ratelimit.client_key(request)
        ok, retry = ratelimit.allow(key)
        return key, (None if ok else retry)

    @app.post("/login")
    def login_post(request: Request, password: str = Form(...)):
        s = _settings()
        key, retry = _login_blocked(request)
        if retry:
            db.event("portal", "auth", f"login throttled for {key} "
                                       f"(retry in {retry}s)")
            return templates.TemplateResponse(
                request, "login.html",
                {"error": f"too many failed attempts — try again in {retry}s"},
                status_code=429,
                headers={"Retry-After": str(retry)})
        if s.password_hash and cfg.verify_password(password, s.password_hash):
            ratelimit.success(key)
            db.event("portal", "auth", "password login ok")
            resp = RedirectResponse(url="/", status_code=302)
            resp.set_cookie(cfg.COOKIE_NAME, cfg.make_token(s), httponly=True,
                            samesite="lax", path="/", max_age=cfg.SESSION_TTL)
            return resp
        left = ratelimit.failure(key)
        db.event("portal", "auth", f"failed login from {key} ({left} left)")
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "wrong password" if left else
             "wrong password — locked out after too many attempts"},
            status_code=401)

    @app.post("/api/login")
    def api_login(request: Request, body: LoginBody):
        s = _settings()
        key, retry = _login_blocked(request)
        if retry:
            raise HTTPException(status_code=429,
                                detail=f"too many attempts; retry in {retry}s")
        if s.password_hash and cfg.verify_password(body.password, s.password_hash):
            ratelimit.success(key)
            db.event("portal", "auth", "api login ok")
            resp = JSONResponse({"ok": True})
            resp.set_cookie(cfg.COOKIE_NAME, cfg.make_token(s), httponly=True,
                            samesite="lax", path="/", max_age=cfg.SESSION_TTL)
            return resp
        left = ratelimit.failure(key)
        db.event("portal", "auth", f"failed api login from {key} ({left} left)")
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
        snap = router_mod.snapshot()
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"providers": providers, "health": health,
                                           "r": snap, "comfy": comfy_snapshot(),
                                           "settings": db.current_settings()})

    @app.get("/models")
    def models(request: Request, _=Depends(_require_auth)):
        p = get_provider("llamacpp")
        live = p.models_live() if p else []
        disk = p.models_scan() if p else []
        for m in disk:
            m["size_h"] = _human(m.get("size"))
        cp = get_provider("comfyui")
        comfy = cp.inventory() if cp else []
        for c in comfy:
            c["size_h"] = _human(c["size"])
            for f in c["files"]:
                f["size_h"] = _human(f["size"])
        return templates.TemplateResponse(request, "models.html",
                                          {"live": live, "disk": disk,
                                           "diskroot": str(paths.LLAMA_MODELS),
                                           "comfy": comfy,
                                           "comfy_categories": cp.categories() if cp else [],
                                           "comfyroot": str(paths.COMFY_MODELS),
                                           "home": str(paths.HOME)})

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

    @app.get("/settings")
    def settings_page(request: Request, _=Depends(_require_auth)):
        return templates.TemplateResponse(request, "settings.html", {})

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
    def api_router(raw: bool = False, _=Depends(_require_auth)):
        """Live router state: service, models, loaded, and the pulse (§6.9).

        The raw journal tail is left out unless ``?raw=1`` — the dashboard polls
        this every few seconds, and the pane that reads it is off by default.
        """
        return router_mod.snapshot(raw=raw)

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

    # ------------------------------------------------- comfyui live hero
    @app.get("/api/comfyui")
    def api_comfyui_live(_=Depends(_require_auth)):
        """Live ComfyUI snapshot for the dashboard hero (mirrors /api/router)."""
        return comfy_snapshot()

    # ------------------------------------------- comfyui (E: service + models)
    @app.post("/api/services/comfyui/ensure")
    def api_comfy_ensure(_=Depends(_require_auth)):
        """Generate the ComfyUI unit + env file + wrapper (idempotent)."""
        res = get_provider("comfyui").ensure_service(
            log=lambda m: print("[ai-portal]", m))
        db.event("portal", "service", "comfyui service files generated")
        return res

    # NOTE on route order: /api/services/{key}/{action} (further down) matches
    # ANY two-segment path under /api/services/, so every specific route here
    # must be registered before it — FastAPI takes the first match, and the
    # generic route's required body turns a shadowed endpoint into a confusing
    # 422. tests/test_routing.py asserts this holds.
    @app.post("/api/services/llamacpp/ensure")
    def api_llama_ensure(_=Depends(_require_auth)):
        """Generate the router's env file + wrapper + unit (idempotent, no
        restart) and verify the wrapper argv without loading a model.

        This is how a hand-written unit is migrated onto the generated pattern:
        install (previous unit file kept in state/rollback/units), then restart
        the router when convenient — the running process keeps its current
        config until then, so this call is always safe.
        """
        res = get_provider("llamacpp").ensure_service(
            log=lambda m: print("[ai-portal]", m))
        db.event("portal", "service",
                 f"llamacpp service files generated (argv ok: {res['verify']['ok']})")
        if not res["verify"]["ok"]:
            raise HTTPException(500, res)
        return res

    @app.get("/api/services/comfyui/config")
    def api_comfy_config(_=Depends(_require_auth)):
        return get_provider("comfyui").config()

    @app.post("/api/services/comfyui/config")
    def api_comfy_set_config(body: ComfyConfigBody, _=Depends(_require_auth)):
        """Validate + persist config, regenerate the env file; restart if the
        service is running so the new args take effect."""
        p = get_provider("comfyui")
        merged = {**p.config(), **body.model_dump(exclude_none=True)}
        try:
            cfg = p.validate_config(merged)
        except ValueError as e:
            raise HTTPException(400, str(e))
        p.set_config(cfg)
        st = systemd.unit_state(paths.COMFY_UNIT)
        restarted = False
        if st.get("ActiveState") == "active":
            systemd.service_action("comfyui", "restart")
            restarted = True
        db.event("portal", "service", f"comfyui config updated: {cfg}")
        return {"config": cfg, "restarted": restarted}

    @app.get("/api/models/comfyui")
    def api_comfy_models(_=Depends(_require_auth)):
        p = get_provider("comfyui")
        return {"root": str(paths.COMFY_MODELS),
                "categories": p.inventory()}

    @app.post("/api/models/comfyui/download")
    def api_comfy_download(body: ComfyDownloadBody, _=Depends(_require_auth)):
        """Portal-side download into the right models/<category>/ folder —
        the file never touches the browser."""
        p = get_provider("comfyui")
        try:
            dest_dir, dest = p.download_target(body.url, body.category,
                                               body.filename)
        except ValueError as e:
            raise HTTPException(400, str(e))
        jid = updater.start_job(
            "comfyui-download", body.url,
            lambda log: p.download_file(body.url, dest_dir, dest, log))
        if jid is None:
            raise HTTPException(409, "a download is already running")
        db.event("portal", "download", f"comfyui {body.url} -> {dest}")
        return {"job": jid, "dest": str(dest)}

    @app.post("/api/models/upload")
    async def api_upload_model(file: UploadFile = File(...),
                               _=Depends(_require_auth)):
        """Upload a model file to staging, then run it through the same
        sniff → place → index pipeline as any other source."""
        dest = (paths.STATE / "downloads" /
                f"upload-{os.getpid()}-{int(time.time() * 1000)}")
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / (file.filename or "upload.bin")
        with open(p, "wb") as f:
            shutil.copyfileobj(file.file, f)
        job_id = updater.start_job(
            "model-add", p.name,
            lambda log: modeladd.add_model(
                modeladd.parse_source(path=str(p)), log=log))
        if job_id is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(status_code=409,
                                detail="a model add is already running")
        db.event("portal", "model-add", f"upload {p.name}")
        return {"job": job_id, "path": str(p)}

    @app.post("/api/models/add")
    def api_add_model(body: AddModelBody, _=Depends(_require_auth)):
        """Portal-side model acquisition: parse the pasted source (HF CLI
        command, repo name, direct URL, local path), fetch it, sniff the
        format and file it in the right store — the file never touches the
        browser. Progress via /api/jobs/{id}."""
        try:
            src = modeladd.parse_source(url=body.url, path=body.path,
                                        hf_cli=body.hf_cli,
                                        hf_repo=body.hf_repo)
        except ValueError as e:
            raise HTTPException(400, str(e))
        jid = updater.start_job("model-add", src["label"],
                                lambda log: modeladd.add_model(
                                    src, filename=body.filename,
                                    category=body.category,
                                    model_dir=body.model_dir, log=log))
        if jid is None:
            raise HTTPException(409, "a model add is already running")
        db.event("portal", "model-add", src["label"])
        return {"job": jid, "source": src}

    @app.post("/api/models/search")
    def api_search_models(body: SearchModelsBody, _=Depends(_require_auth)):
        """Search Hugging Face models by query, service type, and model type.
        Returns a list of ModelInfo summaries (id, tags, pipeline_tag, downloads, likes)."""
        try:
            import huggingface_hub

            # Build filter parameters based on service type
            filters = []
            if body.service == "llamacpp":
                # GGUF models for llama.cpp
                filters.extend(["gguf", "llama.cpp"])
            elif body.service == "comfyui":
                # safetensors models for ComfyUI
                if body.model_type in ["text-to-image", "image-to-image", "text-to-video"]:
                    filters.extend(["diffusers", "safetensors"])
                else:
                    filters.extend(["transformers", "safetensors"])
            else:
                # Default: both GGUF and safetensors
                pass

            # Search models
            models = huggingface_hub.list_models(
                search=body.query,
                pipeline_tag=body.model_type if body.model_type in [
                    "text-generation", "feature-extraction", "image-to-text",
                    "text-to-image", "image-to-image", "audio-to-text"
                ] else None,
                filter=filters if filters else None,
                limit=body.limit,
                full=True,
            )

            results = []
            for m in models:
                # Check if model has GGUF or safetensors files
                has_gguf = getattr(m, 'gguf', None) is not None
                has_safetensors = getattr(m, 'safetensors', None) is not None

                results.append({
                    "id": m.id,
                    "tags": getattr(m, 'tags', []),
                    "pipeline_tag": getattr(m, 'pipeline_tag', None),
                    "downloads": getattr(m, 'downloads', 0),
                    "likes": getattr(m, 'likes', 0),
                    "has_gguf": has_gguf,
                    "has_safetensors": has_safetensors,
                })

            return {"results": results, "count": len(results)}
        except Exception as e:
            raise HTTPException(500, f"search failed: {str(e)}")

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

    # ------------------------------------------------ updates (C: llama.cpp)
    @app.get("/api/updates/llamacpp")
    def api_updates(_=Depends(_require_auth)):
        """Current release, repo head, known releases, and the last job."""
        p = get_provider("llamacpp")
        return p.update_status()

    @app.post("/api/updates/llamacpp/check")
    def api_update_check(_=Depends(_require_auth)):
        """Force a fresh upstream probe: one git fetch, then local counts of
        how far the deployed release is behind master and the latest tag."""
        p = get_provider("llamacpp")
        st = p.remote_state(force=True)
        db.event("portal", "update",
                 f"llamacpp: manual upstream check "
                 f"(behind master {st.get('behind_master')}, "
                 f"release {st.get('latest_release')})")
        return st

    @app.post("/api/updates/llamacpp")
    def api_update(body: UpdateBody, _=Depends(_require_auth)):
        """Start the build→stage→verify→swap→restart pipeline in the
        background; poll /api/jobs/{id} for progress. The click IS the
        confirmation (PORTAL_PLAN §6)."""
        p = get_provider("llamacpp")
        try:
            plan = p.preflight()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        job_id = updater.start_job("llamacpp", body.target,
                                   lambda log: p.update(
                                       body.target, restart=body.restart,
                                       smoke=body.smoke, dry=body.dry, log=log))
        if job_id is None:
            raise HTTPException(status_code=409, detail="an update is already running")
        db.event("portal", "update",
                 f"llamacpp: job {job_id} started (target={body.target}, "
                 f"restart={body.restart}, dry={body.dry})")
        return {"job": job_id, "head": plan["head"]}

    @app.post("/api/updates/llamacpp/rollback")
    def api_rollback(body: RollbackBody, _=Depends(_require_auth)):
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        p = get_provider("llamacpp")
        try:
            res = p.rollback(body.release, log=lambda m: print("[ai-portal]", m))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        db.event("portal", "update", f"llamacpp: rollback -> {body.release}")
        return res

    @app.post("/api/updates/llamacpp/delete")
    def api_delete_release(body: RollbackBody, _=Depends(_require_auth)):
        """Delete a stored build that is not the current one. Irreversible: the
        UI confirms with the build's details first, and confirm must be true
        here too (defence in depth, not a formality — see PORTAL_PLAN §6)."""
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        p = get_provider("llamacpp")
        if not hasattr(p, "delete_release"):
            raise HTTPException(status_code=501, detail="not supported for this service")
        try:
            res = p.delete_release(body.release, log=lambda m: print("[ai-portal]", m))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        db.event("portal", "update",
                 f"llamacpp: deleted build {body.release} "
                 f"({res.get('freed_bytes', 0) / 1048576:.1f} MiB freed, "
                 f"{res.get('remaining')} remain)")
        return res

    # --------------------------------------------- updates (E: ComfyUI)
    @app.get("/api/updates/comfyui")
    def api_comfy_updates(_=Depends(_require_auth)):
        """Installed commit, upstream distance, recent commits, last job."""
        p = get_provider("comfyui")
        return p.update_status()

    @app.post("/api/updates/comfyui/check")
    def api_comfy_update_check(_=Depends(_require_auth)):
        """Force a fresh ``git fetch`` and re-count commits behind master."""
        p = get_provider("comfyui")
        st = p.remote_state(force=True)
        db.event("portal", "update",
                 f"comfyui: manual upstream check "
                 f"(behind master {st.get('behind')})")
        return st

    @app.post("/api/updates/comfyui")
    def api_comfy_update(body: ComfyUpdateBody, _=Depends(_require_auth)):
        """git pull --ff-only + venv requirements sync + restart, in the
        background; poll /api/jobs/{id} for progress."""
        p = get_provider("comfyui")
        if not (paths.COMFY_REPO / ".git").exists():
            raise HTTPException(status_code=400,
                                detail="ComfyUI repo not found")
        job_id = updater.start_job("comfyui", "master",
                                   lambda log: p.update(
                                       restart=body.restart, log=log))
        if job_id is None:
            raise HTTPException(status_code=409,
                                detail="an update is already running")
        db.event("portal", "update",
                 f"comfyui: job {job_id} started (restart={body.restart})")
        return {"job": job_id}

    @app.post("/api/updates/comfyui/rollback")
    def api_comfy_rollback(body: ComfyRollbackBody, _=Depends(_require_auth)):
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        p = get_provider("comfyui")
        try:
            res = p.rollback(body.commit,
                             log=lambda m: print("[ai-portal]", m))
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        db.event("portal", "update", f"comfyui: rollback -> {body.commit[:12]}")
        return res

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int, _=Depends(_require_auth)):
        """Live progress for a background update job (status + log tail)."""
        j = updater.job(job_id)
        if j is None:
            raise HTTPException(status_code=404, detail="no such job")
        return j

    # ---------------------------------------- shared service environment
    @app.get("/api/service-env")
    def api_service_env(_=Depends(_require_auth)):
        """Read the shared runtime env file (~/ai/config/ai-env.env)."""
        return {"env": serviceenv.read_env(), "path": str(paths.AI_ENV_FILE)}

    @app.put("/api/service-env")
    def api_service_env_set(body: dict, _=Depends(_require_auth)):
        """Update one or more variables in the shared env file.

        Body: {"LD_LIBRARY_PATH": "/opt/rocm/core-10.0/lib", ...}
        Does NOT auto-restart services — caller must restart each unit.
        """
        if not body:
            raise HTTPException(status_code=400, detail="empty body")
        env = serviceenv.read_env()
        for k, v in body.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise HTTPException(status_code=400, detail=f"invalid key: {k}")
            env[k] = str(v)
        serviceenv.write_env(env)
        db.event("portal", "config", f"service-env updated: {list(body.keys())}")
        return {"ok": True, "env": env}

    @app.get("/api/settings/services")
    def api_settings_services(_=Depends(_require_auth)):
        """Per-service host/port (current values from the env files) plus the
        shared runtime environment — data for the Settings page."""
        return {"services": serviceconf.service_ports(),
                "shared_env": serviceenv.read_env()}

    @app.get("/api/settings/services/{service}/validate")
    def api_settings_validate(service: str, port: int, _=Depends(_require_auth)):
        """Validate a candidate port for ``service`` without persisting it."""
        if service not in serviceconf.SERVICE_CONF:
            raise HTTPException(404, f"unknown service {service!r}")
        return serviceconf.validate_port(port, service)

    @app.post("/api/settings/services/{service}")
    def api_settings_set_service(service: str, body: dict,
                                 _=Depends(_require_auth)):
        """Validate + persist a host/port change for one service.

        Body: ``{"port": 8081, "external": true}`` (either field optional).
        Does NOT restart the unit — the Settings page shows a restart button
        so the new values take effect when the user is ready.
        """
        if service not in serviceconf.SERVICE_CONF:
            raise HTTPException(404, f"unknown service {service!r}")
        res = serviceconf.set_service_port(service, port=body.get("port"),
                                           external=body.get("external"))
        if not res.get("ok"):
            raise HTTPException(422, {"ok": False, "errors": res["errors"],
                                      "warnings": res["warnings"]})
        db.event("portal", "config",
                 f"service {service}: host={res['host']} port={res['port']} "
                 f"external={res['external']}")
        return res

    # ------------------------------------------- network exposure (LAN access)
    @app.get("/api/network")
    def api_network(_=Depends(_require_auth)):
        """Everything the Settings page needs to answer "can other machines
        reach this service?": interfaces, firewall state, per-service bind +
        live reachability probes, and the router's auth status."""
        return {
            "interfaces": network.interfaces(),
            "primary_lan": network.primary_lan(),
            "lan_network": network.lan_network(),
            "firewall": network.firewall_status(),
            "services": serviceconf.exposure(),
            "router_auth": llamakey.status(),
            "modes": list(network.MODES),
        }

    @app.get("/api/exposure")
    def api_exposure(service: str | None = None, _=Depends(_require_auth)):
        """Bind + reachability report (one service or all three)."""
        if service and service not in serviceconf.SERVICE_CONF:
            raise HTTPException(404, f"unknown service {service!r}")
        return serviceconf.exposure(service)

    @app.post("/api/settings/services/{service}/bind")
    def api_settings_bind(service: str, body: BindBody,
                          _=Depends(_require_auth)):
        """Persist how a service binds to the network — loopback, one LAN
        interface, or all interfaces — and optionally restart the unit.

        Restarting is opt-in and, for the router, needs ``confirm``: restarting
        it drops every agent session riding it (PORTAL_PLAN §4.3).
        """
        if service not in serviceconf.SERVICE_CONF:
            raise HTTPException(404, f"unknown service {service!r}")
        mode = body.bind
        if mode is None and body.external is not None:
            mode = "all" if body.external else "loopback"
        if mode is None:
            raise HTTPException(400, {"ok": False,
                                      "errors": ["bind is required "
                                                 f"({'|'.join(network.MODES)})"]})
        try:
            res = serviceconf.set_bind(service, mode, body.address)
        except ValueError as e:
            raise HTTPException(400, {"ok": False, "errors": [str(e)]})
        if not res.get("ok"):
            raise HTTPException(422, res)
        db.event("portal", "network",
                 f"service {service}: bind {res['previous']} -> {res['host']} "
                 f"({res['mode']})")
        if body.restart:
            if service == "llamacpp" and not body.confirm:
                raise HTTPException(400, {"ok": False,
                                          "errors": ["restarting the llama.cpp "
                                                     "router drops every agent "
                                                     "session riding it — send "
                                                     "confirm=true"]})
            act = systemd.service_action(service, "restart")
            res["restart"] = act
            db.event("portal", "network",
                     f"restart {service} to apply bind {res['host']}: "
                     f"{'ok' if act.get('ok') else act.get('error')}")
        return res

    # ------------------------------------------- llama.cpp router API key
    @app.get("/api/settings/llamacpp/apikey")
    def api_router_key_status(_=Depends(_require_auth)):
        """Router auth state: configured (launch env), enforced (live probe),
        and the key itself — this console is single-operator, so the value is
        returned rather than masked; the endpoint is behind the app password."""
        st = llamakey.status()
        st["key"] = llamakey.reveal() if st["configured"] else None
        if st["configured"]:
            base = serviceconf.base_url("llamacpp", 8080)
            st["web_ui_url"] = llamakey.web_ui_url(base)
        return st

    @app.post("/api/settings/llamacpp/apikey")
    def api_router_key(body: ApiKeyBody, _=Depends(_require_auth)):
        """generate | rotate | revoke the shared router key (launch env only —
        applying it means restarting the router, which needs a confirm)."""
        action = (body.action or "generate").lower()
        if action == "generate":
            res = llamakey.generate(force=False)
        elif action == "rotate":
            res = llamakey.generate(force=True)
        elif action == "revoke":
            res = llamakey.revoke()
        else:
            raise HTTPException(400, f"unknown action {action!r} "
                                     "(generate|rotate|revoke)")
        db.event("portal", "config",
                 f"llamacpp api-key: {action} "
                 f"({'created' if res.get('created') else 'unchanged'})")
        return res

    # ---------------------------------------- huggingface CLI + library
    def _hf_cli_venv() -> Path | None:
        """The dedicated venv the ``hf`` CLI lives in (shebang of ~/.local/bin/hf)."""
        hf_bin = paths.HOME / ".local" / "bin" / "hf"
        if not hf_bin.exists():
            return None
        try:
            shebang = hf_bin.read_text().splitlines()[0].lstrip("#!")
            vpy = Path(shebang.strip())
            return vpy.parent.parent  # <venv>/bin/python -> <venv>
        except Exception:  # noqa: BLE001
            return None

    def _pypi_latest(name: str) -> tuple[str | None, bool]:
        """(latest_version, stale). Offline -> (None, True)."""
        try:
            r = httpx.get(f"https://pypi.org/pypi/{name}/json", timeout=8)
            r.raise_for_status()
            return r.json().get("info", {}).get("version"), False
        except Exception:  # noqa: BLE001
            return None, True

    def _ver_tuple(v: str | None) -> tuple:
        if not v:
            return ()
        out = []
        for part in re.split(r"[.+-]", v):
            out.append(int(part) if part.isdigit() else 0)
        return tuple(out)

    @app.get("/api/hf/status")
    def api_hf_status(force: bool = False, _=Depends(_require_auth)):
        """Versions of the hf CLI, the hub library (portal venv + OS python),
        cache stats, and whether PyPI has a newer huggingface-cli."""
        p = get_provider("huggingface")
        st = p.status()
        detail = st.detail or {}

        cli_version: str | None = None
        venv = _hf_cli_venv()
        if venv and (venv / "bin" / "python").exists():
            try:
                out = subprocess.run(
                    [str(venv / "bin" / "python"), "-c",
                     "import importlib.metadata as m; "
                     "print(m.version('huggingface_hub'))"],
                    capture_output=True, text=True, timeout=20)
                cli_version = out.stdout.strip() or None
            except Exception:  # noqa: BLE001
                pass

        os_hub: str | None = None
        try:
            out = subprocess.run(
                ["/usr/bin/python3", "-c",
                 "import huggingface_hub; print(huggingface_hub.__version__)"],
                capture_output=True, text=True, timeout=20)
            os_hub = out.stdout.strip() or None
        except Exception:  # noqa: BLE001
            pass

        # the `hf` CLI ships inside huggingface_hub; that's what we compare
        latest, stale = _pypi_latest("huggingface_hub")
        update_available = bool(
            cli_version and latest and _ver_tuple(latest) > _ver_tuple(cli_version))

        return {
            "cli_path": str(paths.HOME / ".local" / "bin" / "hf"),
            "cli_venv": str(venv) if venv else None,
            "cli_version": cli_version,
            "hub_portal": getattr(sys.modules.get("huggingface_hub", None), "__version__", None),
            "hub_os": os_hub,
            "python_portal": sys.version.split()[0],
            "cache": detail.get("cache"),
            "cache_size": detail.get("cache_size"),
            "repos": detail.get("repos", []),
            "pypi_latest": latest,
            "stale": stale,
            "update_available": update_available,
        }

    @app.post("/api/updates/hf")
    def api_hf_update(_=Depends(_require_auth)):
        """Upgrade huggingface-cli in its dedicated venv (background job)."""
        venv = _hf_cli_venv()
        if not venv or not (venv / "bin" / "python").exists():
            raise HTTPException(status_code=400,
                                detail="hf CLI venv not found (~/.hf-cli/venv)")

        def run(log):
            log(f"upgrading huggingface_hub (hf CLI) in {venv}")
            out = subprocess.run(
                [str(venv / "bin" / "python"), "-m", "pip", "install",
                 "--upgrade", "huggingface_hub"],
                capture_output=True, text=True, timeout=600)
            for ln in (out.stdout or "").splitlines()[-25:]:
                log(ln)
            if out.returncode != 0:
                raise RuntimeError("pip upgrade failed:\n" +
                                   (out.stderr or "")[-800:])
            v = subprocess.run(
                [str(venv / "bin" / "python"), "-c",
                 "import importlib.metadata as m; "
                 "print(m.version('huggingface_hub'))"],
                capture_output=True, text=True, timeout=20)
            log("done: huggingface_hub " + (v.stdout.strip() or "upgraded"))
            return "huggingface_hub upgraded"

        job_id = updater.start_job("huggingface", "upgrade-hf-cli", run)
        if job_id is None:
            raise HTTPException(status_code=409, detail="an update is already running")
        db.event("portal", "update", f"huggingface: job {job_id} started")
        return {"job": job_id}

    # ---------------------------------------- GPU / ROCm / SDK probes
    @app.get("/api/gpu")
    def api_gpu(_=Depends(_require_auth)):
        """Read-only probe of the AMD stack: ROCm core version under
        /opt/rocm, amdgpu/amdrocm package counts, Vulkan build SDK.
        OS-managed (amdgpu-install) — reported, not updated here."""
        rocm = Path("/opt/rocm")
        cores = []
        if rocm.is_dir():
            for d in sorted(rocm.glob("core-*")):
                cores.append(d.name.replace("core-", ""))
        pkg_counts = {}
        for pat in ("amdgpu*", "amdrocm*"):
            try:
                out = subprocess.run(["dpkg-query", "-W", pat],
                                     capture_output=True, text=True, timeout=15)
                pkg_counts[pat] = len([l for l in out.stdout.splitlines() if l.strip()])
            except Exception:  # noqa: BLE001
                pkg_counts[pat] = None
        return {
            "rocm_version": cores[-1] if cores else None,
            "rocm_cores": cores,
            "rocm_packages": pkg_counts,
            "vulkan_sdk": str(paths.VULKAN_SDK).replace(str(paths.HOME), "~"),
            "radv_driver": "system (mesa amdgpu/radv via Ubuntu + ROCm)",
        }

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
