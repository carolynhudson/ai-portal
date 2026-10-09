"""FastAPI application: auth, HTML pages (HTMX+Jinja), and the JSON API.

Frontend note (decision D1): the UI is HTMX + Jinja with *no JS build step*.
Every read is also exposed as a ``/api/*`` JSON endpoint, and templates may
embed small inline ``<script>`` blocks, so adding a chart (e.g. CPU/RAM) is
just a ``<canvas>`` + a few lines of JS that ``fetch()`` an existing endpoint —
no front-end re-engineering.
"""
from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__, metrics, providers
from . import router as router_mod
from .core import config as cfg
from .core import (
    db,
    health,
    kb,
    llamakey,
    logsetup,
    model_config,
    modeladd,
    modelindex,
    network,
    paths,
    personality,
    presets_store,
    ratelimit,
    serviceconf,
    serviceenv,
    settingrows,
    systemd,
    updater,
)
from .providers import PROVIDERS
from .providers import get as get_provider

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


class PersonalityApplyBody(BaseModel):
    """Which personality to write, optional edits on top of it, and explicit consent to write it.

    `confirm` defaults to False so a request that omits it is a refusal rather than an apply —
    the same rule as a router stop or a release delete (see PORTAL_PLAN §6).

    `global_edits` and `model_edits` are what the settings editor sends. They are raw on purpose:
    a form field is a string, and coercing strings into registry types is `personality`'s job with
    rules the same page documents, not something pydantic should decide by inferring a type here.
    """
    name: str
    global_edits: dict[str, str] = {}
    model_edits: dict[str, dict[str, str]] = {}
    confirm: bool = False


class PresetsRollbackBody(BaseModel):
    """Restore a named presets.ini snapshot. `confirm` for the same reason as above: this
    replaces a file the router reads and restarts the router."""
    stamp: str
    confirm: bool = False


class PersonalitySaveBody(BaseModel):
    """Create or replace a custom personality.

    No `confirm`: this writes the portal's own definition table, not presets.ini, and it restarts
    nothing. `models_max` stays an int here rather than a string because it is the one value the
    portal itself parses and acts on (`_models_max` refuses the rest), and `models` is a list of
    library model ids and/or stored model config names — `None` meaning the whole library, which is
    what `default` means.
    """
    name: str
    models_max: int = 1
    note: str = ""
    models: list[str] | None = None
    global_edits: dict[str, str] = {}


class ModelConfigSaveBody(BaseModel):
    """Create or update a model config: a name the router will answer to, the library model it
    renames, and the settings that model gets. Values arrive raw for the same reason as an apply
    payload — a form field is a string and the registry decides the type."""
    name: str
    base_model: str
    note: str = ""
    settings: dict[str, str] = {}


class NamedBody(BaseModel):
    """Delete one personality or model config. `confirm` because a delete changes what the next
    apply would write, and because a config name is something other rows already reference."""
    name: str
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
    #: ``--upgrade`` the packages requirements.txt names. Off by default:
    #: ComfyUI's own instruction is to satisfy requirements, and this machine's
    #: ROCm torch is held either way (see providers/comfyui.write_constraints).
    upgrade: bool = False


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


def _num(v) -> float | None:
    """The value as a finite float, or None when there is no number to show.

    Durations and sizes reach these display helpers from timestamp arithmetic,
    journal gaps and cgroup counters, any of which can yield NaN or inf (a clock
    stepped backwards, a counter that wrapped) — and `int(NaN)`/`int(inf)` raise,
    so the isfinite() check is what keeps a bad counter from 500ing a page.

    Everything else renders as nothing: None, a bool, a string from a release
    registry, and Jinja's `Undefined` all mean "there is no number here", and the
    callers turn None into an em dash. `Undefined` is treated as absent rather
    than re-raised deliberately — these helpers are registered as Jinja filters,
    and it is the template that decides whether a missing key is acceptable (it
    guards with `is not none` where it matters). A filter that 500s on Undefined
    takes that decision away from the template and turns every optional field
    into a crash, which is what an earlier version of this line did to six tests.
    """
    if v is None or isinstance(v, bool) or _jinja_undefined(v):
        return None
    # int and float only, not the whole numbers.Real hierarchy: a Decimal or
    # Fraction would want different formatting anyway, and naming the concrete
    # types is what makes the float() below unremarkable.
    if isinstance(v, (int, float)):
        # float(int)/float(float) are total — no value of either type raises,
        # including ±1e308 — and non-finite results are dropped by isfinite()
        # below, so there is nothing a try/except could catch here.
        # pi-lens-ignore: unchecked-throwing-call-python
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def _jinja_undefined(v) -> bool:
    """True for a Jinja Undefined. Imported lazily so non-web callers of _num
    (and the module itself, when used as a library) do not depend on jinja2."""
    try:
        from jinja2 import Undefined
    except ImportError:  # pragma: no cover - jinja2 ships with fastapi's family
        return False
    return isinstance(v, Undefined)


def _dur(seconds: object) -> str:
    """'12d 4h' / '38m' / 'just now' — spans, not ages (uptime, elapsed).

    `seconds` is typed loosely on purpose: this is a Jinja filter and callers
    hand it whatever a probe returned, including nothing. See _num().
    """
    val = _num(seconds)
    if val is None:
        return "—"
    # pi-lens-ignore: unchecked-throwing-call-python
    s = int(val)
    if s < 5:
        return "just now"
    if s < 3600:
        return f"{s // 60}m"
    d, rem = divmod(s, 86400)
    h, m = divmod(rem // 60, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _ago(hours: object) -> str:
    """'20m' / '3h' / '12d' — the health pane answers "how fresh is this?" in
    the same shape everywhere, because it asks it about a dozen things.
    Loosely typed for the same reason as _dur: it is a filter, not an API."""
    hours = _num(hours)
    if hours is None:
        return "—"
    if hours < 1:
        # pi-lens-ignore: unchecked-throwing-call-python
        return f"{max(0, int(hours * 60))}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


_UNITS = ("B", "K", "M", "G", "T", "P", "E")


def _ctxlen(n: object) -> str:
    """A token count as "131K" / "1.0M". Context lengths cluster at powers of two
    in the tens of thousands to millions, where bytes formatting happens to give
    the right shape but the wrong unit name — `_human(131072)` says "128K", which
    reads like a size and not like a context window.

    0 renders as "0", not an em dash: a zero trained context is absent data in the
    header and worth seeing as odd, whereas `est_ctx == 0` means "never fits" and
    must not disappear into the same glyph as unknown. Callers distinguish those;
    the formatter does not invent a distinction it cannot see.
    """
    v = _num(n)
    if v is None:
        return "—"
    if v == 0:
        return "0"
    if v >= 1048576:
        return f"{v / 1048576:.1f}M"
    if v >= 1024:
        return f"{v / 1024:.0f}K"
    return f"{v:.0f}"


def _model_row_limits(index_rows: list[dict]) -> dict[str, dict[str, str]]:
    """Per model id: which editor rows that file cannot use, and the fact it is missing.

    Computed from the index on the server, because the naming rules live in the row layer
    (`settingrows.VISIBLE_FACTS`) and a browser copy of them is a second, weaker statement about
    the same files -- the same class of duplication §6.14.1 exists to delete. The reason text comes
    from `settingrows.hidden_reason`, so what the page says a row needs is exactly what the row
    layer checks, and a row that vanishes says why it vanished instead of reading as a build that
    lacks the setting.
    """
    rows = settingrows.editable_rows()
    out: dict[str, dict[str, str]] = {}
    for mrow in index_rows:
        limits = {}
        for r in rows:
            why = settingrows.hidden_reason(r, mrow)
            if why:
                limits[r["key"]] = why
        out[str(mrow.get("id"))] = limits
    return out


def _human(n: object) -> str:
    """Byte count as "71.0G". Anything we cannot read as a finite number — None,
    zero, a string from a registry entry, an inf from a counter that wrapped —
    reads as an em dash, because "—" is the honest answer to "how big?" when the
    answer is "we do not know", and a display helper must not be what 500s a page.

    Above exabytes the ladder runs out and the answer is a literal ">1E": the
    old five-unit version fell out of its loop and returned a bare `str(float)`,
    so an absurd size printed a few hundred digits on a status line.
    """
    size = _num(n)
    if not size:
        return "—"
    for unit in _UNITS:
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    # Past exabytes the unit ladder is simply out of road. Say so rather than
    # print the number: 1e308 bytes rendered as 290 digits, which is worse than
    # useless on a status line and was the old five-unit version's fallback too.
    return ">1E"


def _discard_upload(dest: Path) -> None:
    """Remove an upload's staging directory, best effort.

    A partial model file is multi-GB and the caller is about to raise, so this
    runs on every failure path — but a failed cleanup must not replace the error
    the caller is trying to report.

    Both halves of that are explicit rather than inherited from the flag: `ignore_errors=True`
    swallows the errors rmtree collects internally (verified on 3.14.4 — a missing path does
    not raise, an unreadable directory returns None), and the `except` below is the backstop
    for anything rmtree itself can raise before it gets to that point, such as a bad argument.
    Discarding it is deliberate: this runs while the caller is already raising, and a cleanup
    traceback must not replace the error being reported.
    """
    try:
        shutil.rmtree(dest, ignore_errors=True)
    except OSError as exc:
        logsetup.get().debug("upload cleanup skipped for %s: %s", dest, exc)


def _busy(kinds: set[str] | None = None) -> HTTPException:
    """The 409 for "the machine is busy", naming what is busy.

    A refusal that says only "already running" leaves the operator guessing
    whether to wait a minute or twenty, and for what.
    """
    return HTTPException(status_code=409,
                         detail="busy — " + updater.busy_reason(kinds))


def _refuse_if_busy(kinds: set[str] | None = None) -> None:
    """Raise the 409 if a job currently owns the machine (default: any job)."""
    if updater.blocker(kinds):
        raise _busy(kinds)


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
    p = providers.get_llamacpp()
    res = p.behind() if p else {}
    _behind_cache = (now, res)
    return res


def comfy_snapshot() -> dict:
    """Live ComfyUI state for the dashboard hero (mirrors router_mod.snapshot()).

    Probes the endpoint as configured (see :meth:`ComfyUI.endpoint`) and shows
    the bind, so the hero says the same thing the Settings page does about who
    can reach it.
    """
    p = providers.get_comfyui()
    cfg = p.config()
    st = systemd.unit_state(paths.COMFY_UNIT)
    base = p.endpoint()
    web = router_mod.probe(f"{base}/system_stats")
    version = None
    if web["ok"]:
        with contextlib.suppress(Exception):
            version = httpx.get(f"{base}/system_stats",
                                timeout=3).json()["system"]["comfyui_version"]
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


#: Warm-up threads started by `create_app()`, kept reachable so a test can join the one it started
#: instead of racing it. They are daemon threads: nothing in production waits for them, which is
#: precisely what makes them a problem in a test suite that shares one `model_index` table.
INDEX_STARTUP_THREADS: list[threading.Thread] = []


def create_app() -> FastAPI:
    log = logsetup.setup()          # level: AI_PORTAL_LOG_LEVEL (env/unit)
    app = FastAPI(title="AI Portal", version="0.1.0")
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    # the health pane asks "how big / how old" about a dozen things; formatting
    # them once here beats a dozen inconsistent template expressions
    templates.env.filters["ago"] = _ago
    templates.env.filters["dur"] = _dur
    templates.env.filters["sz"] = _human
    templates.env.filters["ctxlen"] = _ctxlen
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
    log.info("AI Portal %s: home=%s ai_root=%s state=%s",
             app.version, paths.HOME, paths.AI_ROOT, paths.PORTAL_STATE)

    # The model index (§4.9 N1) has to actually exist for anything that reads it
    # to work, and it must not be a table someone remembers to build by hand. It is
    # created here and populated off the request path: a scan of ~15 headers is
    # sub-second, but a scan of a library on a cold mount or a slow disk must not
    # be able to stall a page load. Failures are logged and leave the index empty,
    # which every reader already treats as "fall back to walking the disk".
    def _index_startup() -> None:
        try:
            # _settings() is what runs db.init() (schema + first-run password),
            # and it is idempotent. rescan() writes an `events` row, so it needs
            # the base schema to exist first — calling it before the first request
            # used to fail with "no such table: events", which silently left the
            # index empty forever.
            _settings()
            modelindex.init()
            r = modelindex.rescan()
            log.info("model index: %s model(s), %s problem(s)",
                     r["models"], len(r["problems"]))
        except Exception:  # noqa: BLE001 — index absence is survivable by design
            log.exception("model index scan failed; readers fall back to disk")

    # Switchable for exactly one reason. This thread rescans whatever `app.core.paths` points at at
    # the moment it happens to run. Pointed at a sandboxed home, that is a warm-up. Pointed at the
    # home some other test file has monkeypatched, it rewrites a table underneath a test that is
    # asserting on that table's contents -- which is how tests in `test_personality.py` started failing
    # at random depending on which file ran before them. The suite turns it off (tests/conftest.py)
    # and the two tests that care about the startup scan turn it back on and wait for it.
    if os.environ.get("AI_PORTAL_INDEX_STARTUP", "1") != "0":
        warmup = threading.Thread(target=_index_startup, daemon=True,
                                  name="model-index-startup")
        INDEX_STARTUP_THREADS.append(warmup)
        warmup.start()

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
        providers = {p.key: _provider_card(p.status()) for p in PROVIDERS}
        # local named hb, not health: `health` is the probe module (Phase F), and
        # shadowing a module with a dict in one route is how the next person's
        # NameError happens
        hb = _health_bundle()
        snap = router_mod.snapshot()
        # The machine detail section (disk, GPU) renders once, at page load: these are
        # seconds-scale numbers with no second-scale signal in them. They carry the time
        # they were read, and the poll below reports the time of its own sample.
        msnap = metrics.snapshot()
        disk = [[k, f"{_human(v['used'])} / {_human(v['total'])} ({v['percent']:.0f}%)"]
                for k, v in (msnap["disk"] or {}).items() if v]
        gpu = msnap.get("gpu")
        gpu_txt = ("GPU {use_percent}%".format(use_percent=gpu["use_percent"])
                   if gpu and gpu.get("use_percent") is not None else "n/a (rocm-smi)")
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"providers": providers, "health": hb,
                                           "r": snap, "comfy": comfy_snapshot(),
                                           "disk": disk, "gpu": gpu_txt,
                                           "sampled": datetime.now().strftime("%H:%M:%S"),
                                           "settings": db.current_settings()})

    @app.get("/models")
    def models(request: Request, _=Depends(_require_auth)):
        p = providers.get_llamacpp()
        live = p.models_live() if p else []
        disk = p.models_scan() if p else []
        for m in disk:
            m["size_h"] = _human(m.get("size"))
        # The header index (§4.9 Phase N) is what answers "can it run here"; the
        # directory listing above cannot, because it only knows filenames. Read
        # only — a page load must never be able to change what is on disk — and it
        # reads the table, it does not rescan: a scan belongs on startup and on
        # explicit action, not on every render of a listing.
        budget = modelindex._pool_bytes()
        try:
            indexed = modelindex.rows()
        except Exception:  # noqa: BLE001 — the page still renders without the index
            indexed = []
        # Which library rows are also live right now, matched on a key both sides
        # agree on. The router registers some models under a HuggingFace-style alias
        # (`unsloth/X-GGUF:Q8_K_XL`) that no directory name resembles, so raw string
        # comparison left a serving model looking idle — the one error a "live" light
        # must not make, because a wrong "not running" invites loading a second copy
        # of a model that is already taking traffic.
        try:
            live_by_key = modelindex.live_keys(live)
        except Exception:  # noqa: BLE001 — the light is a convenience; the page is not
            live_by_key = {}
        library_ids = set()
        # Cache rows in the same repo compete for the same id; the page has to say
        # so before a personality bakes one in (modelindex.id_round_trip).
        siblings = modelindex.cache_sibling_map(indexed)
        for m in indexed:
            m["verdict"] = modelindex.verdict(m, budget)
            # Derived here, not stored: one rule for what a file can do (§4.9.2).
            m["caps"] = modelindex.capabilities(
                m, siblings.get(str(m["id"]).rsplit(":", 1)[0]))
            hit = live_by_key.get(modelindex.router_key(m["id"]))
            m["live"] = bool(hit)
            m["live_as"] = (hit or {}).get("id") or ""
            m["peak_h"] = _human(modelindex.peak_bytes(m))
            library_ids.add(modelindex.router_key(m["id"]))
        # Live models the library does not know: served from outside the scanned tree,
        # or an alias whose weights live under a different name. Listed rather than
        # dropped, because "the router is serving something I cannot see in my library"
        # is a fact an operator needs, and hiding it makes the merged table a lie.
        extra_live = [m for m in live
                      if modelindex.router_key(m.get("id") or "") not in library_ids]
        pool_h = _human(budget)
        g = metrics.gpu() if hasattr(metrics, "gpu") else None
        if g and g.get("is_igpu"):
            pool_h = f"{pool_h} GTT"
        # Scan problems are not persisted (rescan returns them, the health pane
        # recomputes them), so this page does not claim a count it does not have.
        # The health page is where incomplete shard sets and dead symlinks live.
        cp = providers.get_comfyui()
        comfy = cp.inventory() if cp else []
        for c in comfy:
            c["size_h"] = _human(c["size"])
            for f in c["files"]:
                f["size_h"] = _human(f["size"])
        return templates.TemplateResponse(request, "models.html",
                                          {"live": live, "disk": disk,
                                           "indexed": indexed,
                                           "extra_live": extra_live,
                                           "live_n": sum(1 for m in indexed if m["live"]),
                                           "pool_h": pool_h,
                                           "unknown_n": sum(
                                               1 for m in indexed
                                               if m["verdict"] == "unknown"),
                                           "diskroot": str(paths.LLAMA_MODELS),
                                           "comfy": comfy,
                                           "comfy_categories": cp.categories() if cp else [],
                                           "comfyroot": str(paths.COMFY_MODELS),
                                           "home": str(paths.HOME)})

    @app.get("/settings")
    def settings_page(request: Request, _=Depends(_require_auth)):
        return templates.TemplateResponse(request, "settings.html", {})

    @app.get("/help")
    def help_page(request: Request, q: str = "", _=Depends(_require_auth)):
        """The project's own docs, searchable, addressed by the numbers the UI cites.

        A setting's help says "§6.12.6"; this page is what that pointer means. Search returns
        ranked sections, and a section id is a stable address -- parsed from the heading, so a
        page link cannot drift away from the document it names.
        """
        hits = kb.search(q, limit=40) if q.strip() else []
        return templates.TemplateResponse(request, "help.html", {
            "q": q,
            "hits": hits,
            "toc": kb.toc(),
            "docs": kb.docs_available(),
            "count": len(kb.sections()),
        })

    def _health_page(refresh: bool = False) -> dict:
        """Health pane data: host probes plus the deployed build's provenance.

        The unit states come from systemd directly rather than from a provider:
        this page has to keep answering when a service is down, which is exactly
        when it gets read.
        """
        router_u = systemd.unit_state(paths.ROUTER_UNIT)
        comfy_u = systemd.unit_state(paths.COMFY_UNIT)
        p = providers.get_llamacpp()
        # Values are str-or-None by design: `built_ago` and friends are absent
        # rather than guessed when a release record predates the field.
        prov: dict[str, str | None] = {}
        try:
            prov = p.provenance() if p else {}
        except Exception as e:  # noqa: BLE001 — a broken release is a row, not a 500
            prov = {"error": f"{type(e).__name__}: {e}"}
        # Unregistered builds belong to the provider, but the health page must
        # still render if the provider throws — a broken release directory is a
        # row on this page, not a 500.
        try:
            builds = p.stray_builds() if p else {}
        except Exception as e:  # noqa: BLE001
            builds = {"strays": [], "unexpected": [], "bytes": 0,
                      "error": f"{type(e).__name__}: {e}"}
        body = health.summary(
            release=({"rocm": prov.get("rocm_built"), "name": prov.get("name")}
                     if prov.get("name") else None),
            service={"state": router_u.get("ActiveState", "unknown")},
            comfy={"state": comfy_u.get("ActiveState", "unknown")},
            builds=builds,
            refresh=refresh)
        # "built 2026-09-11T13:41:12+00:00" is not a fact a person reads; the
        # age is, and the raw stamp stays in the payload for any JSON consumer
        prov["built_ago"] = None
        try:
            built = datetime.fromisoformat(str(prov.get("built_at")))
            prov["built_ago"] = _ago((time.time() - built.timestamp()) / 3600)
        except (TypeError, ValueError):
            pass
        body["deployed"] = prov
        body["builds"] = builds
        body["host"] = health.host()
        return body

    @app.get("/health")
    def health_page(request: Request, refresh: bool = False,
                    _=Depends(_require_auth)):
        """What this machine needs from its operator (PORTAL_PLAN §4.5, F).

        Read-only by construction: the only control on the page is a re-check.
        Applying updates, rebooting and deleting builds stay on the Status page
        and in Settings behind their own confirms — so this pane can be read
        without fear of what reading it might do.
        """
        return templates.TemplateResponse(request, "health.html",
                                          _health_page(refresh))

    # ----------------------------------------------------------------- JSON
    def _health_bundle() -> dict:
        rp = systemd.reboot_pending()
        behind = _llama_behind()
        disk = metrics.disk()
        # The aperture, not rocm-smi's VRAM: on an iGPU the latter is a ~512 MiB
        # stub while the models live in ~108 GiB of GTT (§6.12.0). This is the
        # pool a model either fits into or does not.
        g = health.gpu_aperture()
        gpu_mem = (f"{_human(g.get('pool_used'))} / {_human(g.get('pool_total'))}"
                   if g and g.get("pool_total") else "—")
        bundle = {
            "reboot_required": rp.get("reboot_required", False),
            "vulkan_sdk": str(paths.VULKAN_SDK).replace(str(paths.HOME), "~"),
            "llama_local": behind.get("local", "—"),
            "llama_origin": behind.get("origin", "—") or "—",
            "llama_behind": behind.get("behind", "—"),
            "ai_disk": f"{_human(disk.get('ai', {}).get('used'))} / {_human(disk.get('ai', {}).get('total'))}" if disk.get("ai") else "—",
            "root_disk": f"{_human(disk.get('root', {}).get('used'))} / {_human(disk.get('root', {}).get('total'))}" if disk.get("root") else "—",
            "gpu_mem": gpu_mem,
        }
        return bundle

    @app.get("/api/help")
    def api_help(q: str = "", _=Depends(_require_auth)):
        return {"query": q, "hits": kb.search(q, limit=40)}

    @app.get("/api/help/section/{ref}")
    def api_help_section(ref: str, _=Depends(_require_auth)):
        """One section's rendered body. 404 is the honest answer to a citation of nothing."""
        s = kb.find(ref)
        if not s:
            raise HTTPException(status_code=404, detail=f"no documented section {ref!r}")
        return {"id": s["id"], "doc": s["doc"], "title": s["title"], "level": s["level"],
                "html": kb.render_markdown(s["body"])}

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

    @app.get("/personalities")
    def personalities_page(request: Request, _=Depends(_require_auth)):
        """Personalities (§4.7): what each one allows, what the worst case costs, and what
        presets.ini would look like if it were applied.

        This page is read-only even though the write path now exists (D7). Applying is a separate
        endpoint — POST /api/personalities/apply — gated on an explicit confirm and on the machine
        being otherwise idle, so the page a person looks at while deciding cannot also be the thing
        that decides for them. Rendering here has no side effects: it does not write, snapshot, or
        restart.
        """
        budget = modelindex._pool_bytes()
        # Read once: the editor's model list and the per-model row limits must come from the same
        # index snapshot, or the page can offer a model its own limits table does not know.
        index_rows = modelindex.rows()
        people = personality.all_personalities()
        cards = [personality.describe(p, budget) for p in people]
        generated = []
        for p in people:
            try:
                g = personality.render(p, budget)
                # "Active" is read off the file, not remembered. render() already compares what this
                # personality would write against what is on disk, so a personality is active exactly
                # when its file is the file -- which is also why a hand-edited presets.ini shows up
                # as "nothing is active" rather than as a personality the router is not running.
                g["active"] = not g.get("changed", True)
                generated.append(g)
            except personality.PersonalityError as exc:
                # A personality that cannot be rendered is reported as such on its own
                # card. Dropping the card would make the page look like the personality
                # does not exist, which is a different and worse lie.
                generated.append({"name": p.get("name"), "error": str(exc), "diff": [],
                                  "text": "", "changed": True, "active": False,
                                  "models": [], "budget": {}})
        # The card carries the live-state fact, so the page states it in exactly one place per card.
        # The deck needs it on the strip and the body needs it under the fold; two Jinja conditions
        # reading `generated` at two positions is what made the last deck attempt look as if two
        # identical conditions disagreed (§6.14.1c Findings 1-2). One value, computed here, read
        # there.
        for card, g in zip(cards, generated, strict=True):
            card["active"] = bool(g.get("active"))
        configs = model_config.rows()
        config_by_name = {c["name"]: c for c in configs}
        # What the editor must show as the personality's current configuration. Built here, from
        # the same personality dicts the cards were built from, so the form and the card cannot
        # describe two different personalities (§6.14.1i).
        config_views = {p.get("name"): personality.settings_view(p, configs=config_by_name)
                        for p in people}
        return templates.TemplateResponse(request, "personalities.html",
                                          {"cards": cards, "generated": generated,
                                           "configs": configs,
                                           "config_views": config_views,
                                           "config_uses": {c["name"]: model_config
                                                           .assigned_personalities(c["name"])
                                                           for c in configs},
                                           # Each key's citation, reduced to the section id the help page
                                           # opens. A key whose cite names no section gets no link: a
                                           # link to nothing is worse than a link that 404s.
                                           "help_refs": {k: kb.ref_of(v.get("cite"))
                                                        for k, v in personality.SETTINGS.items()},
                                           "pool_h": _human(budget),
                                           # The editor's model list comes from the index rather than
                                           # from the personality cards: a per-model section only means
                                           # something if it names a directory the router can see, and
                                           # normalize_proposed() refuses any name that does not.
                                           "model_ids": [r["id"] for r in index_rows],
                                           # The editor grids render rows, not SETTINGS lookups: control
                                           # shape, default, unit, rule, help link and visibility facts
                                           # all arrive on the row (app/core/settingrows.py, §6.14.1).
                                           "editor_rows": settingrows.editable_rows(),
                                           "model_row_limits": _model_row_limits(index_rows),
                                           "router_unit": paths.ROUTER_UNIT,
                                           "presets_path": str(paths.PRESETS_INI),
                                           # The keys table renders one row type from the registry's row
                                           # data (app/core/settingrows.py, plan §6.14.1): kind, scope,
                                           # rule, default, unit, aliases, help link and visibility facts
                                           # all arrive on the row, so the page cannot explain a key
                                           # differently from the way the generator validates it.
                                           "keys_rows": settingrows.rows(),
                                           "home": str(paths.HOME)})

    @app.get("/api/personalities")
    def api_personalities(_=Depends(_require_auth)):
        """JSON for the same view (D1: every read is also an endpoint)."""
        budget = modelindex._pool_bytes()
        configs = model_config.rows()
        people = personality.all_personalities()
        config_by_name = {c["name"]: c for c in configs}
        return {
            "budget_bytes": budget,
            "personalities": [personality.describe(p, budget) for p in people],
            "personalities_on_disk": [p["name"] for p in personality.stored()],
            "configs": configs,
            # The same current-configuration view the editor seeds from, so a script driving the
            # API and a person reading the page answer "what does this personality set?" from one
            # place (D1: every read is also an endpoint).
            "config_views": {p.get("name"): personality.settings_view(p, configs=config_by_name)
                             for p in people},
            "assigned": {c["name"]: model_config.assigned_personalities(c["name"]) for c in configs},
            "settings": _setting_specs(),
        }

    @app.post("/api/personalities/preview")
    def api_personality_preview(body: PersonalityApplyBody,
                               _=Depends(_require_auth)):
        """What applying a personality would do, without doing any of it.

        Returns the diff, the budget verdict, and the reasons it would refuse. The UI calls this
        before it enables an Apply button, so an operator sees the exact file and the exact cost
        before anything is gated. Nothing here writes or snapshots. `check_generated()` runs so a
        refusal is reported here rather than discovered at apply time.
        """
        try:
            persona = _personality_by_name(body.name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        budget = modelindex._pool_bytes()
        try:
            # Editor edits are validated before anything is rendered, so a bad value is reported
            # as a refused field here rather than as a file that would not generate.
            persona = personality.normalize_proposed(persona, body.global_edits, body.model_edits)
            rendered = personality.render(persona, budget)
        except personality.PersonalityError as e:
            raise HTTPException(400, str(e)) from None
        known = {r["id"] for r in modelindex.rows()}
        return {
            "name": rendered["name"],
            "models": rendered["models"],
            "models_max": rendered["models_max"],
            "diff": rendered["diff"],
            "changed": rendered["changed"],
            "budget": rendered["budget"],
            "would_refuse": personality.check_generated(rendered["text"], known),
        }

    @app.post("/api/personalities/apply")
    def api_personality_apply(body: PersonalityApplyBody, _=Depends(_require_auth)):
        """Apply a personality to presets.ini, through the gated job system.

        Gated three ways, because this is the only write in the portal that changes what the
        router loads:

          * `confirm` must be true — same rule as a router stop or a release delete;
          * the machine must be otherwise idle (the job kind is in updater.DISRUPTIVE, since the
            apply restarts ai-llama-router);
          * personality.apply() refuses on its own before touching disk if the plan does not fit,
            cannot be priced, or contains a key the registry rejects.

        Runs as a job rather than inline: the restart takes longer than a request should hold a
        connection, and progress must survive the browser going away.
        """
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        try:
            persona = _personality_by_name(body.name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        try:
            persona = personality.normalize_proposed(persona, body.global_edits, body.model_edits)
        except personality.PersonalityError as e:
            raise HTTPException(400, str(e)) from None
        jid = updater.start_job("presets-apply", body.name,
                                lambda log: personality.apply(
                                    persona, modelindex._pool_bytes(), log=log))
        if jid is None:
            raise _busy()
        db.event("portal", "presets-apply-started", f"personality={body.name} job={jid}")
        return {"job": jid, "personality": body.name}

    @app.get("/api/personalities/snapshots")
    def api_presets_snapshots(_=Depends(_require_auth)):
        """Every presets.ini that can be rolled back to, newest first.

        Read-only and deliberately free of any diff body: the page needs to show what exists and
        how big it is. `readable: false` is surfaced rather than filtered out, because a backup
        that exists and cannot be read is the one an operator most needs to know about.
        """
        return {"snapshots": presets_store.list_snapshots(),
                "path": str(paths.PRESETS_INI)}

    @app.post("/api/personalities/rollback")
    def api_presets_rollback(body: PresetsRollbackBody, _=Depends(_require_auth)):
        """Restore a snapshot, then restart the router so it actually re-reads the file.

        Restoring the file without restarting would leave the router running the configuration the
        rollback was meant to undo, which is the failure mode a rollback must not have.
        """
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        jid = updater.start_job("presets-apply", f"rollback {body.stamp}",
                                lambda log: _rollback_presets(body.stamp, log))
        if jid is None:
            raise _busy()
        db.event("portal", "presets-rollback-started",
                 f"stamp={body.stamp} job={jid}")
        return {"job": jid, "stamp": body.stamp}

    def _rollback_presets(stamp: str, log) -> dict:
        """Job body: restore a snapshot, verify it landed, restart the router."""
        try:
            before = presets_store.snapshot(reason=f"before rollback {stamp}",
                                            personality="(rollback)")
            restored = presets_store.restore(stamp)
        except presets_store.PresetsSnapshotError as e:
            raise RuntimeError(str(e)) from e
        log(f"restored presets.ini from snapshot {stamp} ({restored['bytes']} bytes)")
        db.event("portal", "presets-rollback",
                 f"stamp={stamp} undo={before['stamp']} bytes={restored['bytes']}")
        state = systemd.service_action("llamacpp", "restart")
        if not state.get("ok"):
            log(f"router restart FAILED: {state.get('error')}; presets.ini is restored and "
                f"snapshot {before['stamp']} can undo this rollback")
        else:
            log(f"restarted {paths.ROUTER_UNIT}")
        return {**restored, "restart": state}

    def _personality_by_name(name: str) -> dict:
        """Resolve a personality name to the definition the generator will use.

        Built-ins and stored ones resolve identically, which is the point: a custom personality is
        not a second kind of personality with a second set of rules, it is the same shape coming
        from the database instead of from the module. An unknown name raises so the route answers
        404 rather than silently applying something else.
        """
        for p in personality.all_personalities():
            if p.get("name") == name:
                return dict(p)
        raise ValueError(f"no personality named {name!r}")

    def _setting_specs() -> list[dict]:
        """The registry as data: what every writable key means, what it accepts, what it costs.

        `accepts` is left out because it is a callable, and a JSON projection of a callable is a
        string nobody can act on. The rules themselves stay in Python where the generator uses them.
        """
        return [{"key": k, **{f: v for f, v in spec.items() if f != "accepts"}}
                for k, spec in personality.SETTINGS.items()]

    @app.get("/api/model-configs")
    def api_model_configs(_=Depends(_require_auth)):
        """Stored model configs, the library models they may be built on, and who uses them.

        `assigned` is part of the read rather than something the delete route works out later: the
        page has to show that deleting `tiny-chat` would take the settings and the name away from
        `work`, before the operator asks it to.
        """
        configs = model_config.rows()
        return {
            "configs": configs,
            "bases": [r["id"] for r in modelindex.rows()],
            "assigned": {c["name"]: model_config.assigned_personalities(c["name"]) for c in configs},
            "settings": _setting_specs(),
        }

    @app.post("/api/model-configs/save")
    def api_model_config_save(body: ModelConfigSaveBody, _=Depends(_require_auth)):
        """Save one model config. Refuses before writing: a name the router would collide with, a
        base model that is not in the library, or a value the registry refuses.

        Not gated on `confirm` or on the machine being idle: this changes a definition the portal
        keeps, not the file the router reads. presets.ini only changes through Apply.
        """
        try:
            clean = personality.normalize_config(body.name, body.base_model, body.settings,
                                                 body.note)
            saved = model_config.save(clean["name"], clean["base_model"], clean["settings"],
                                      clean["note"])
        except (personality.PersonalityError, model_config.ConfigError) as e:
            raise HTTPException(400, str(e)) from None
        return {"config": saved,
                "assigned": model_config.assigned_personalities(saved["name"]),
                "personalities": [p["name"] for p in personality.stored()]}

    @app.post("/api/model-configs/delete")
    def api_model_config_delete(body: NamedBody, _=Depends(_require_auth)):
        """Delete a model config, unless a personality is still using it.

        The refusal is the interesting half. A config that vanishes from under a personality does
        not error anywhere: the personality simply stops carrying those settings and the next apply
        writes a smaller file than the operator believes they are editing.
        """
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        users = model_config.assigned_personalities(body.name)
        if users:
            raise HTTPException(400, f"{body.name!r} is assigned by {', '.join(users)}; remove it "
                                     f"from those personalities first")
        if not model_config.delete(body.name):
            raise HTTPException(404, f"no model config named {body.name!r}")
        return {"deleted": body.name}

    @app.post("/api/personalities/save")
    def api_personality_save(body: PersonalitySaveBody, _=Depends(_require_auth)):
        """Create or replace a custom personality.

        `safe` and `default` are refused by name: they are derived from the library, which is what
        makes them a floor a bad edit can be recovered to. Saving through here still cannot reach
        presets.ini — that stays Apply's job.
        """
        try:
            saved = personality.save_personality(
                body.name, body.models_max, body.note, body.models, body.global_edits,
                modelindex._pool_bytes())
        except personality.PersonalityError as e:
            raise HTTPException(400, str(e)) from None
        return {"personality": personality.describe(saved, modelindex._pool_bytes()),
                "personalities": [p["name"] for p in personality.stored()]}

    @app.post("/api/personalities/delete")
    def api_personality_delete(body: NamedBody, _=Depends(_require_auth)):
        """Delete a custom personality. `safe` and `default` are refused rather than absent: the
        page should say why the button is not there instead of hiding it."""
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        try:
            if not personality.delete_personality(body.name):
                raise HTTPException(404, f"no personality named {body.name!r}")
        except personality.PersonalityError as e:
            raise HTTPException(400, str(e)) from None
        return {"deleted": body.name}

    @app.get("/api/models")
    def api_models(_=Depends(_require_auth)):
        p = providers.get_llamacpp()
        return {"live": p.models_live() if p else [],
                "disk": p.models_scan() if p else []}

    @app.get("/api/metrics")
    def api_metrics(_=Depends(_require_auth)):
        return metrics.snapshot()

    @app.get("/api/health")
    def api_health(_=Depends(_require_auth)):
        return _health_bundle()

    @app.get("/api/health/detail")
    def api_health_detail(refresh: bool = False, _=Depends(_require_auth)):
        """The health pane as JSON (same bundle as /health, no HTML)."""
        return _health_page(refresh)

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
        res = providers.get_comfyui().ensure_service(
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
        res = providers.get_llamacpp().ensure_service(
            log=lambda m: print("[ai-portal]", m))
        db.event("portal", "service",
                 f"llamacpp service files generated (argv ok: {res['verify']['ok']})")
        if not res["verify"]["ok"]:
            raise HTTPException(500, res)
        return res

    @app.get("/api/services/comfyui/config")
    def api_comfy_config(_=Depends(_require_auth)):
        return providers.get_comfyui().config()

    @app.post("/api/services/comfyui/config")
    def api_comfy_set_config(body: ComfyConfigBody, _=Depends(_require_auth)):
        """Validate + persist config, regenerate the env file; restart if the
        service is running so the new args take effect."""
        p = providers.get_comfyui()
        merged = {**p.config(), **body.model_dump(exclude_none=True)}
        try:
            cfg = p.validate_config(merged)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
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
        p = providers.get_comfyui()
        return {"root": str(paths.COMFY_MODELS),
                "categories": p.inventory()}

    @app.post("/api/models/comfyui/download")
    def api_comfy_download(body: ComfyDownloadBody, _=Depends(_require_auth)):
        """Portal-side download into the right models/<category>/ folder —
        the file never touches the browser."""
        p = providers.get_comfyui()
        try:
            dest_dir, dest = p.download_target(body.url, body.category,
                                               body.filename)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        jid = updater.start_job(
            "comfyui-download", body.url,
            lambda log: p.download_file(body.url, dest_dir, dest, log))
        if jid is None:
            raise _busy()
        db.event("portal", "download", f"comfyui {body.url} -> {dest}")
        return {"job": jid, "dest": str(dest)}

    @app.post("/api/models/upload")
    async def api_upload_model(file: UploadFile = File(...),
                               _=Depends(_require_auth)):
        """Upload a model file to staging, then run it through the same
        sniff → place → index pipeline as any other source."""
        # One staging directory per request: the pid keeps two portal processes
        # from sharing a name and the timestamp keeps two requests apart.
        # time_ns() rather than int(time.time() * 1000) — same uniqueness, and no
        # float-to-int conversion that a reader has to reason about.
        dest = (paths.STATE / "downloads" /
                f"upload-{os.getpid()}-{time.time_ns()}")
        # A multipart body carries a client-supplied filename, and writing an
        # arbitrarily large model can fail on a full disk. Neither is the app's
        # fault, and neither should surface as an unhandled 500 — nor leave a
        # partial multi-GB upload behind in state/downloads, which is why the
        # staging directory is removed on every failure path.
        # basename() so a client cannot aim the write outside staging with a
        # path in its filename field (verified: '../../etc/passwd' -> 'passwd').
        name = Path(file.filename or "upload.bin").name
        try:
            dest.mkdir(parents=True, exist_ok=True)
            p = dest / name
            with open(p, "wb") as f:
                shutil.copyfileobj(file.file, f)
        except OSError as e:
            _discard_upload(dest)
            # A full disk is a machine condition and deserves its own status;
            # anything else about the request is the client's to fix.
            code = 507 if e.errno == errno.ENOSPC else 400
            raise HTTPException(status_code=code,
                                detail=f"could not store upload: {e}") from e
        except ValueError as e:
            _discard_upload(dest)
            raise HTTPException(status_code=400,
                                detail=f"invalid upload: {e}") from e
        job_id = updater.start_job(
            "model-add", p.name,
            lambda log: modeladd.add_model(
                modeladd.parse_source(path=str(p)), log=log))
        if job_id is None:
            _discard_upload(dest)
            raise _busy()
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
            raise HTTPException(400, str(e)) from None
        jid = updater.start_job("model-add", src["label"],
                                lambda log: modeladd.add_model(
                                    src, filename=body.filename,
                                    category=body.category,
                                    model_dir=body.model_dir, log=log))
        if jid is None:
            raise _busy()
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
            raise HTTPException(500, f"search failed: {str(e)}") from None

    @app.post("/api/services/{key}/{action}")
    def api_service_action(key: str, action: str, body: ServiceActionBody,
                           _=Depends(_require_auth)):
        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=404, detail="unknown action")
        if key not in systemd.MANAGED_UNITS:
            raise HTTPException(status_code=404, detail="unknown service")
        if action in ("stop", "restart") and not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        # Stopping a service that an update is mid-way through swapping, or
        # restarting the portal and killing the thread that is building it, are
        # both ways to end up with half-applied state. Wait for the job.
        if action in ("stop", "restart"):
            _refuse_if_busy(updater.DISRUPTIVE)
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
        p = providers.get_llamacpp()
        return p.update_status()

    @app.post("/api/updates/llamacpp/check")
    def api_update_check(_=Depends(_require_auth)):
        """Force a fresh upstream probe: one git fetch, then local counts of
        how far the deployed release is behind master and the latest tag."""
        p = providers.get_llamacpp()
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
        p = providers.get_llamacpp()
        # busy first, validate second: whether the machine can take this at all
        # is the more useful answer, and preflight costs a git probe
        _refuse_if_busy()
        try:
            plan = p.preflight()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        job_id = updater.start_job("llamacpp", body.target,
                                   lambda log: p.update(
                                       body.target, restart=body.restart,
                                       smoke=body.smoke, dry=body.dry, log=log))
        if job_id is None:
            raise _busy()
        db.event("portal", "update",
                 f"llamacpp: job {job_id} started (target={body.target}, "
                 f"restart={body.restart}, dry={body.dry})")
        return {"job": job_id, "head": plan["head"]}

    @app.post("/api/updates/llamacpp/rollback")
    def api_rollback(body: RollbackBody, _=Depends(_require_auth)):
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        _refuse_if_busy(updater.DISRUPTIVE)
        p = providers.get_llamacpp()
        try:
            res = p.rollback(body.release, log=lambda m: print("[ai-portal]", m))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from None
        db.event("portal", "update", f"llamacpp: rollback -> {body.release}")
        return res

    @app.post("/api/updates/llamacpp/delete")
    def api_delete_release(body: RollbackBody, _=Depends(_require_auth)):
        """Delete a stored build that is not the current one. Irreversible: the
        UI confirms with the build's details first, and confirm must be true
        here too (defence in depth, not a formality — see PORTAL_PLAN §6).

        Gated like a rollback: deleting a tree while a job is swapping services
        around is the same class of accident."""
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        _refuse_if_busy(updater.DISRUPTIVE)
        p = providers.get_llamacpp()
        if not hasattr(p, "delete_release"):
            raise HTTPException(status_code=501, detail="not supported for this service")
        try:
            res = p.delete_release(body.release, log=lambda m: print("[ai-portal]", m))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from None
        db.event("portal", "update",
                 f"llamacpp: deleted build {body.release} "
                 f"({res.get('freed_bytes', 0) / 1048576:.1f} MiB freed, "
                 f"{res.get('remaining')} remain)")
        return res

    @app.post("/api/updates/llamacpp/purge")
    def api_purge_build(body: RollbackBody, _=Depends(_require_auth)):
        """Delete an **unregistered** build directory: one left on disk by an
        update that died before it recorded anything, so it is in no list and
        `delete build` refuses it by design.

        Separate endpoint rather than a relaxation of `delete`, because its
        authority is different — the registry cannot vouch for these, so the
        provider re-derives safety from disk (name shape, containment, the bin
        farm, running processes) and refuses if any of it is unverifiable.
        """
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        _refuse_if_busy(updater.DISRUPTIVE)
        p = providers.get_llamacpp()
        try:
            res = p.purge_stray(body.release, log=lambda m: print("[ai-portal]", m))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        db.event("portal", "update",
                 f"llamacpp: purged UNREGISTERED build {body.release} "
                 f"({res.get('freed_bytes', 0) / 1048576:.1f} MiB, "
                 f"{res.get('files')} files; record: {res.get('note_dir')})")
        return res

    # --------------------------------------------- updates (E: ComfyUI)
    @app.get("/api/updates/comfyui")
    def api_comfy_updates(_=Depends(_require_auth)):
        """Installed commit, upstream distance, recent commits, last job."""
        p = providers.get_comfyui()
        return p.update_status()

    @app.post("/api/updates/comfyui/check")
    def api_comfy_update_check(_=Depends(_require_auth)):
        """Force a fresh ``git fetch`` and re-count commits behind master."""
        p = providers.get_comfyui()
        st = p.remote_state(force=True)
        db.event("portal", "update",
                 f"comfyui: manual upstream check "
                 f"(behind master {st.get('behind')})")
        return st

    @app.post("/api/updates/comfyui")
    def api_comfy_update(body: ComfyUpdateBody, _=Depends(_require_auth)):
        """git pull --ff-only + venv requirements sync + restart, in the
        background; poll /api/jobs/{id} for progress."""
        p = providers.get_comfyui()
        if not (paths.COMFY_REPO / ".git").exists():
            raise HTTPException(status_code=400,
                                detail="ComfyUI repo not found")
        job_id = updater.start_job(
            "comfyui", "master" + (" (+upgrade)" if body.upgrade else ""),
            lambda log: p.update(restart=body.restart, upgrade=body.upgrade,
                                 log=log))
        if job_id is None:
            raise _busy()
        db.event("portal", "update",
                 f"comfyui: job {job_id} started (restart={body.restart}, "
                 f"upgrade={body.upgrade})")
        return {"job": job_id}

    @app.post("/api/updates/comfyui/rollback")
    def api_comfy_rollback(body: ComfyRollbackBody, _=Depends(_require_auth)):
        if not body.confirm:
            raise HTTPException(status_code=400, detail="confirm required")
        _refuse_if_busy(updater.DISRUPTIVE)
        p = providers.get_comfyui()
        try:
            res = p.rollback(body.commit,
                             log=lambda m: print("[ai-portal]", m))
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from None
        db.event("portal", "update", f"comfyui: rollback -> {body.commit[:12]}")
        return res

    # declared before /api/jobs/{job_id}: "active" is not an int
    @app.get("/api/jobs/active")
    def api_jobs_active(_=Depends(_require_auth)):
        """What is running right now, for the status bar on every page.

        One SQL query, no provider probes: the bar polls this continuously, and a
        job that finished while you were on another page still has to reach you.
        """
        return updater.payload()

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
            "services": _exposure(),
            "router_auth": llamakey.status(),
            "modes": list(network.MODES),
        }

    def _exposure(service: str | None = None) -> dict:
        """The bind report, joined with the authentication report.

        These two facts live in different places and drift apart: the env file says `0.0.0.0`, the
        router key file may have been revoked since. Reported separately, an open port reads as
        "reachable" and the missing key reads as a pill somewhere else on the page.
        """
        report = serviceconf.exposure(service)
        for key in ([service] if service else list(report)):
            entry = report.get(key)
            if entry is not None:
                entry["auth"] = serviceconf.auth_state(
                    key, key_configured=(llamakey.configured()
                                         if key == "llamacpp" else None))
        return report

    @app.get("/api/exposure")
    def api_exposure(service: str | None = None, _=Depends(_require_auth)):
        """Bind + reachability + auth report (one service or all three)."""
        if service and service not in serviceconf.SERVICE_CONF:
            raise HTTPException(404, f"unknown service {service!r}")
        return _exposure(service)

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
            raise HTTPException(400, {"ok": False, "errors": [str(e)]}) from None
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
        """Version string as a comparable tuple; non-numeric segments sort as 0.

        `int()` is guarded by `isdigit()`, which admits only pure-digit strings —
        so this cannot raise, and the guard is what makes that true rather than
        an accident of the inputs seen so far.
        """
        if not v:
            return ()
        out = []
        for part in re.split(r"[.+-]", v):
            # pi-lens-ignore: unchecked-throwing-call-python
            out.append(int(part) if part.isdigit() else 0)
        return tuple(out)

    @app.get("/api/hf/status")
    def api_hf_status(force: bool = False, _=Depends(_require_auth)):
        """Versions of the hf CLI, the hub library (portal venv + OS python),
        cache stats, and whether PyPI has a newer huggingface-cli."""
        p = providers.get_huggingface()
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
            except Exception:
                cli_version = None  # no hf CLI, or it will not answer: that is the finding

        os_hub: str | None = None
        try:
            out = subprocess.run(
                ["/usr/bin/python3", "-c",
                 "import huggingface_hub; print(huggingface_hub.__version__)"],
                capture_output=True, text=True, timeout=20)
            os_hub = out.stdout.strip() or None
        except Exception:
            os_hub = None  # system python has no huggingface_hub: that is the finding

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
            raise _busy()
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
                pkg_counts[pat] = len([line for line in out.stdout.splitlines() if line.strip()])
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
