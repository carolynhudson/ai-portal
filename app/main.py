"""FastAPI application: auth, HTML pages (HTMX+Jinja), and the JSON API.

Frontend note (decision D1): the UI is HTMX + Jinja with *no JS build step*.
Every read is also exposed as a ``/api/*`` JSON endpoint, and templates may
embed small inline ``<script>`` blocks, so adding a chart (e.g. CPU/RAM) is
just a ``<canvas>`` + a few lines of JS that ``fetch()`` an existing endpoint —
no front-end re-engineering.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import httpx

from . import metrics, router as router_mod
from .core import config as cfg
from .core import db, modeladd, paths, systemd, updater
from .providers import PROVIDERS, get as get_provider

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"


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
    pipeline_tag: str | None = None
    library: str | None = None
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
    """Live ComfyUI state for the dashboard hero (mirrors router_mod.snapshot())."""
    p = get_provider("comfyui")
    cfg = p.config()
    st = systemd.unit_state(paths.COMFY_UNIT)
    web = router_mod.probe(f"http://127.0.0.1:{cfg['port']}/system_stats")
    version = None
    if web["ok"]:
        try:
            version = httpx.get(f"http://127.0.0.1:{cfg['port']}/system_stats",
                                timeout=3).json()["system"]["comfyui_version"]
        except Exception:  # noqa: BLE001
            pass
    return {
        "host": f"http://{cfg['listen']}:{cfg['port']}",
        "port": cfg["port"],
        "service": {"state": st.get("ActiveState", "unknown"),
                    "sub": st.get("SubState"),
                    "since": st.get("ActiveEnterTimestamp", "").split(" ")[0] or None},
        "web": web,
        "version": version,
        "memory": systemd.service_memory(paths.COMFY_UNIT),
        "activity": systemd.journal_tail(paths.COMFY_UNIT, 200)[-80:],
    }


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
        return router_mod.snapshot()

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
        """Search Hugging Face models by query, pipeline tag, and library.
        Returns a list of ModelInfo summaries (id, tags, pipeline_tag, downloads, likes)."""
        try:
            import huggingface_hub

            # Build filter parameters
            filters = []
            if body.pipeline_tag:
                filters.append(body.pipeline_tag)

            # Search models
            models = huggingface_hub.list_models(
                search=body.query,
                pipeline_tag=body.pipeline_tag,
                library=body.library,
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
