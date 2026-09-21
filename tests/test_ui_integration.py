"""UI integration tests — every interactable element, driven in a real DOM.

Why: the portal's UI has no build step and no types shared with the backend, so
the usual "it still imports" tests prove nothing about it. The bugs that
actually ship in a UI like this are wiring bugs — an element id renamed on one
side only, a button wired to a function a template edit deleted, a handler
posting to a route that moved. This file drives each page in jsdom against a
sandboxed portal and clicks, types in, changes and submits every control the
user can reach, then asserts that nothing threw, nothing 404'd, no element id
went missing, no tab lost its panel, no destructive button skipped its
confirmation, and no control was dead.

The portal it drives is a real uvicorn process, but with:
  - ``AI_PORTAL_HOME`` in a temp dir (its whole ``~/ai`` tree is a sandbox)
  - ``tests/ui/bin`` first on PATH, so ``systemctl``/``journalctl`` are shims
  - a dead outbound proxy, so no test reaches the internet
  - mutating requests matched by ``spec.json`` intercepted *in the harness* —
    asserted for shape, never executed

Run:  ./scripts/check-ui.sh
      uv run --with pytest python -m pytest tests/test_ui_integration.py -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
UIDIR = ROOT / "tests" / "ui"
sys.path.insert(0, str(ROOT))

from app.core import personality as _personality  # noqa: E402
from tests.test_modelindex import _write_fake_gguf  # noqa: E402

# Read from the module instead of copying the string: `safe` names one model by hand, and a
# fixture that repeats that string keeps passing when the two disagree.
SAFE_MODEL = _personality.SAFE["models"][0]


def _seed_gguf(path: Path, *, pad: int = 0) -> None:
    """A GGUF the portal's reader can parse, for the sandbox's model library.

    The harness needs index-backed pages (models table, personality budgets) to render
    populated states, and b"\\0" * N is not a GGUF: sniff_gguf rejects it, the row never
    lands in model_index, and the page renders its empty state forever. Reusing the indexer
    test's writer means one fixture definition, so the reader cannot drift from the fixture
    without one of the two suites noticing.
    """
    _write_fake_gguf(path, {
        "block_count": 8,
        "attention.head_count": 4,
        "attention.head_count_kv": 2,
        "embedding_length": 256,
        "context_length": 4096,
    }, pad=pad)
SPEC = json.loads((UIDIR / "spec.json").read_text())
NODE = shutil.which("node")
PASSWORD = "ui-test-password-123"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to drive the DOM tests")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def ui_server():
    """A sandboxed portal on a real socket. Nothing here can reach ~/ai."""
    home = Path(tempfile.mkdtemp(prefix="aipp-ui-home-"))
    syscalls = home / "syscalls.log"

    # A little fake content so the pages render their populated states: an HF
    # cache to scan, a llama model dir, a ComfyUI category.
    (home / ".cache" / "huggingface" / "hub" / "models--x--y").mkdir(parents=True)
    (home / ".cache" / "huggingface" / "hub" / "models--x--y" / "blob").write_bytes(b"x" * 1024)
    # A GGUF the portal's own reader can actually parse. The previous fixture here was 2048
    # null bytes: valid enough to list as a file, not valid enough to sniff, so model_index
    # stayed empty after the startup rescan and any page that reads the index rendered its
    # empty state. That is honest behaviour and a useless UI fixture — a budget figure that
    # is never produced cannot be asserted to appear.
    mdir = home / "ai" / "models" / "llama" / "ui-test-model"
    mdir.mkdir(parents=True)
    _seed_gguf(mdir / "ui-test-model-Q4_K_M.gguf", pad=2048)
    # `safe` names one specific model as the fallback. A sandbox without it is not broken —
    # the page correctly reports that the fallback cannot start — but it renders `safe` with
    # no budget, so a UI assertion about the budget figure would be describing only `default`
    # and would pass even if the fallback card stopped computing one.
    (home / "ai" / "models" / "llama" / SAFE_MODEL).mkdir(parents=True)
    _seed_gguf(home / "ai" / "models" / "llama" / SAFE_MODEL / f"{SAFE_MODEL}.gguf", pad=1024)

    env = dict(os.environ)
    env.update(
        AI_PORTAL_HOME=str(home),
        AI_PORTAL_HOST="127.0.0.1",
        AI_PORTAL_PORT=str(_free_port()),
        AI_PORTAL_PASSWORD=PASSWORD,
        AI_PORTAL_TEST_SYSCALL_LOG=str(syscalls),
        PATH=f"{UIDIR / 'bin'}{os.pathsep}{env['PATH']}",
        # fail fast instead of reaching the internet: the endpoints degrade to
        # their documented offline states, and a UI test must not depend on
        # PyPI or the hub being up
        http_proxy="http://127.0.0.1:1",
        https_proxy="http://127.0.0.1:1",
        HTTP_PROXY="http://127.0.0.1:1",
        HTTPS_PROXY="http://127.0.0.1:1",
        no_proxy="127.0.0.1,localhost",
        NO_PROXY="127.0.0.1,localhost",
    )
    base = f"http://127.0.0.1:{env['AI_PORTAL_PORT']}"
    log = open(home / "server.log", "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", env["AI_PORTAL_PORT"], "--no-access-log"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                log.seek(0)
                pytest.fail(f"sandbox portal exited ({proc.returncode}):\n{log.read()[-3000:]}")
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            pytest.fail("sandbox portal never answered /healthz")

        r = httpx.post(f"{base}/api/login", json={"password": PASSWORD}, timeout=10)
        assert r.status_code == 200, f"sandbox login failed: {r.status_code} {r.text}"
        token = r.cookies.get("aipp")
        assert token, "login did not set the session cookie"
        yield {"base": base, "cookie": f"aipp={token}", "home": home, "syscalls": syscalls}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run_page(ui_server, page: str) -> dict:
    spec = next(p for p in SPEC["pages"] if p["name"] == page)
    cmd = [NODE, str(UIDIR / "run.mjs"),
           "--base", ui_server["base"],
           "--spec", str(UIDIR / "spec.json"),
           "--page", page]
    if spec.get("auth", True):
        cmd += ["--cookie", ui_server["cookie"]]
    out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    raw = out.stdout.strip()
    if not raw:
        pytest.fail(f"node harness produced no output for {page}\ncmd: {' '.join(cmd)}\n"
                    f"stderr:\n{out.stderr[-3000:]}")
    report = json.loads(raw)
    report["_stderr"] = out.stderr[-2000:]
    return report


def _fmt(report: dict) -> str:
    """Everything needed to fix the failure, in one place."""
    lines = [f"page: {report['page']}  url: {report['url']}", ""]
    lines.append("failures:")
    lines += [f"  - {f}" for f in report.get("failures", [])]
    init = report.get("initial", {})
    if init:
        lines += ["", "load errors:  " + json.dumps(init.get("errors", []), indent=2),
                  "console.error: " + json.dumps(init.get("consoleErrors", []), indent=2),
                  "http errors:  " + json.dumps(init.get("httpErrors", [])),
                  "missing ids:  " + json.dumps(init.get("missedIds", []))]
    lines += ["", "requests made:"]
    lines += [f"  {r}" for r in init.get("requests", [])]
    lines += ["", f"controls exercised: {report.get('summary', {})}"]
    dead = [c for c in report.get("controls", []) if c.get("effect") == "none"]
    if dead:
        lines += ["", "dead controls:"]
        lines += [f"  {c['selector']} text={c['text']!r} handler={c['handler']!r}" for c in dead]
    if report.get("_stderr"):
        lines += ["", "harness stderr:", report["_stderr"]]
    return "\n".join(lines)


@pytest.mark.parametrize("page", [p["name"] for p in SPEC["pages"]])
def test_every_control_works(ui_server, page):
    """Load the page, drive it, require zero failures.

    The report's `failures` list is the assertion; it is built by run.mjs from
    the invariants that must hold for any page (see run.mjs for the list).
    """
    report = run_page(ui_server, page)
    assert report["ok"], _fmt(report)


def test_ui_never_asks_systemd_to_start_or_stop_a_unit(ui_server):
    """The suite intercepts service mutations, so the sandbox must never have
    been told to start/stop/restart anything — the guarantee that makes it
    safe to run these tests against a portal that manages live services."""
    # exercise the destructive paths explicitly before asserting
    for page in ("dashboard", "settings"):
        run_page(ui_server, page)
    log = ui_server["syscalls"]
    ran = log.read_text() if log.exists() else ""
    offenders = [ln for ln in ran.splitlines()
                 if re.search(r"\b(systemctl)\b.*\b(start|stop|restart)\b", ln)]
    assert not offenders, "the UI suite reached systemd:\n" + "\n".join(offenders)


def test_every_served_page_has_a_ui_spec():
    """A new page must be added to spec.json, or this UI suite silently stops
    covering the app."""
    os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-ui-routes-"))
    from app.main import app

    served = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        endpoint = getattr(route, "endpoint", None)
        if not path or "GET" not in methods or "{" in path:
            continue
        # only pages the portal itself defines — FastAPI's own /docs, /redoc and
        # the static mount are not UI to spec
        if getattr(endpoint, "__module__", None) != "app.main":
            continue
        if path.startswith("/api") or path == "/healthz":
            continue
        served.add(path)
    specced = {p["path"] for p in SPEC["pages"]}
    assert served <= specced, f"pages served but not covered by tests/ui/spec.json: {sorted(served - specced)}"


def test_ui_requests_only_hit_registered_routes(ui_server):
    """A handler posting to a renamed route fails quietly in the browser and
    loudly here."""
    os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-ui-routes-"))
    from app.main import app

    patterns = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path:
            continue
        for method in (methods or {"GET"}):
            rx = "^" + re.sub(r"\{[^/]+\}", "[^/]+", path.rstrip("/") or "/") + "/?$"
            patterns.append((method, re.compile(rx)))

    unknown = []
    for page in (p["name"] for p in SPEC["pages"]):
        report = run_page(ui_server, page)
        for req in report.get("requestTrace", []):
            if req["status"] in (None, "cross-origin-refused"):
                continue
            if not any(m == req["method"] and rx.match(req["path"]) for m, rx in patterns):
                unknown.append(f"{page}: {req['method']} {req['path']}")
    assert not unknown, "requests to routes the app does not register:\n" + "\n".join(unknown)


def test_destructive_requests_carry_what_the_backend_needs(ui_server):
    """Blocked requests are answered by the harness, so their *shape* is the
    contract: if the UI stopped sending ``confirm`` or the job ``target``,
    the endpoint would reject it in production and nothing here would notice."""
    contracts = {
        "dashboard": [
            (r"^/api/services/[^/]+/(start|stop|restart)$", {"confirm"}),
            (r"^/api/updates/llamacpp$", {"target", "restart", "smoke"}),
            (r"^/api/updates/llamacpp/rollback$", {"release", "confirm"}),
            (r"^/api/updates/comfyui$", {"restart"}),
        ],
        "settings": [
            (r"^/api/settings/services/[^/]+/bind$", {"bind", "address"}),
            (r"^/api/settings/llamacpp/apikey$", {"action"}),
        ],
        "models": [
            (r"^/api/models/search$", {"query", "service", "model_type", "limit"}),
            (r"^/api/models/add$", {"filename", "model_dir", "category"}),
        ],
    }
    seen = set()
    problems = []
    for page, rules in contracts.items():
        report = run_page(ui_server, page)
        # every request the page sent that carried a body, whether the harness
        # blocked it, answered it from a fixture, or forwarded it to the sandbox
        for req in report.get("requestTrace", []):
            if req["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            for pattern, required in rules:
                if not re.match(pattern, req["path"]):
                    continue
                seen.add(f"{page}:{pattern}")
                try:
                    body = json.loads(req["body"] or "{}")
                except json.JSONDecodeError as e:
                    problems.append(f"{page} {req['method']} {req['path']}: body is not JSON ({e})")
                    continue
                missing = [k for k in required if k not in body]
                if missing:
                    problems.append(
                        f"{page} {req['method']} {req['path']}: body missing {missing}: {req['body'][:200]}")
    never_exercised = sorted({
        f"{p}:{pat}" for p, rules in contracts.items() for pat, _ in rules} - seen)
    assert not problems, "destructive request contract broken:\n" + "\n".join(problems)
    assert not never_exercised, (
        "these destructive paths were never exercised by the UI (button removed, "
        "or blocked by an earlier error):\n" + "\n".join(never_exercised))
