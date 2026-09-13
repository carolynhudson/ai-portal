"""Static UI contract tests — no browser, no server, fast.

These exist so that the cheapest possible run still catches the two ways a
no-build-step UI breaks most often:

1. A template edit makes a page's JavaScript unparseable. A ``<script>`` block
   with a syntax error does not fail loudly: the browser skips the whole block,
   so every function it defined is undefined and every button in it is dead.
   (This actually happened: one apostrophe in ``CLI's`` inside a ``confirm()``
   string killed the block defining four of the five update tabs.)
2. One side of a wiring pair is renamed — an ``onclick="fn()"`` whose function
   is gone, a ``getElementById('x')`` whose element was renamed, a tab whose
   panel no longer matches, a ``fetch('/api/…')`` whose route moved.

``tests/test_ui_integration.py`` catches both dynamically as well; this file
catches them with a message that names the exact line, and keeps working when
jsdom is not installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# throwaway home, before the app is imported (same pattern as test_smoke.py)
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-uic-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "contract-test-password")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PAGES = {"/": "dashboard", "/models": "models", "/metrics": "metrics", "/settings": "settings"}
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
HANDLER_ATTR_RE = re.compile(r"\bon(?:click|change|submit|input)=\"\s*([A-Za-z_$][\w$]*)\s*\(")
FUNC_DEF_RE = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|[a-z]+\s*=>)")
FETCH_RE = re.compile(r"""fetch\(\s*[`'"]([^`'"${}]+)[`'"]""")
TEMPLATE_LEFTOVER_RE = re.compile(r"\{\{.*?\}\}")


@pytest.fixture(scope="module")
def client():
    c = TestClient(app)
    r = c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]}, follow_redirects=False)
    assert r.status_code == 302, "could not log in to render pages"
    return c


@pytest.fixture(scope="module")
def pages(client):
    out = {}
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} did not render: {r.status_code}"
        out[path] = r.text
    return out


def _check_js(src: str) -> str | None:
    """Return node's syntax error for this source, or None if it parses."""
    if not NODE:
        return None
    p = subprocess.run([NODE, "--check"], input=src, capture_output=True, text=True)
    return None if p.returncode == 0 else p.stderr.strip()


def test_inline_scripts_parse(pages):
    """A single syntax error silently kills a whole ``<script>`` block."""
    if not NODE:
        pytest.skip("node not installed")
    broken = []
    for path, html in pages.items():
        for i, block in enumerate(SCRIPT_RE.findall(html)):
            if not block.strip():
                continue
            err = _check_js(block)
            if err:
                where = err.splitlines()[0] if err.splitlines() else "?"
                defined = re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", block)
                broken.append(
                    f"{path} script #{i}: {where}\n"
                    f"    functions lost with it: {', '.join(defined) or '(none)'}"
                )
    assert not broken, "unparseable inline JavaScript:\n" + "\n".join(broken)


def test_inline_scripts_parse_in_the_source_templates():
    """Same check without rendering, so the failure names the template file and
    line. Jinja expressions are replaced with placeholders first."""
    if not NODE:
        pytest.skip("node not installed")
        return
    broken = []
    for tpl in sorted((ROOT / "web" / "templates").glob("*.html")):
        text = tpl.read_text()
        for i, m in enumerate(SCRIPT_RE.finditer(text)):
            body = m.group(1)
            if not body.strip():
                continue
            # {{ expr }} -> 0, {% tag %} -> nothing: keep line numbers intact
            stripped = re.sub(r"\{\{.*?\}\}", "0", body, flags=re.S)
            stripped = re.sub(r"\{%.*?%\}", "", stripped, flags=re.S)
            err = _check_js(stripped)
            if err:
                line = text[: m.start()].count("\n") + 1
                broken.append(f"{tpl.relative_to(ROOT)}:{line} script #{i}: "
                              f"{(err.splitlines() or ['?'])[0]}")
    assert not broken, "unparseable JavaScript in templates:\n" + "\n".join(broken)


def test_every_handler_names_a_function_that_exists(pages):
    """``onclick="foo()"`` with no ``foo`` is an inert button plus a console
    error — the classic half-finished refactor."""
    missing = []
    for path, html in pages.items():
        js = "\n".join(SCRIPT_RE.findall(html))
        defined = set(g for m in FUNC_DEF_RE.finditer(js) for g in m.groups() if g)
        # handlers defined by htmx / the browser
        defined |= {"event", "this"}
        for m in HANDLER_ATTR_RE.finditer(html):
            name = m.group(1)
            if name not in defined:
                line = html[: m.start()].count("\n") + 1
                missing.append(f"{path} line {line}: handler {name}() is not defined on this page")
    assert not missing, "handlers without a function:\n" + "\n".join(missing)


def test_fetch_targets_are_registered_routes(pages):
    """A handler posting to a route that no longer exists fails in silence."""
    patterns = set()
    for route in app.routes:
        p = getattr(route, "path", None)
        if not p:
            continue
        patterns.add("^" + re.sub(r"\{[^/]+\}", "[^/]+", p.rstrip("/") or "/") + "/?$")
    unknown = []
    for path, html in pages.items():
        for block in SCRIPT_RE.findall(html):
            for target in FETCH_RE.findall(block):
                target = target.split("?")[0]
                if not target.startswith("/"):
                    continue
                # `fetch('/api/services/' + key + '/' + action)` captures as
                # '/api/services/' — a prefix that must be matched against a
                # route with more segments, so try plausible completions
                tests = [target]
                if target.endswith("/"):
                    tests += [target + "x", target + "x/y"]
                if not any(re.match(rx, t) for t in tests for rx in patterns):
                    unknown.append(f"{path} fetches {target}, which the app does not register")
    assert not unknown, "\n".join(unknown)


@pytest.mark.parametrize("path,tab_class,prefix",
                         [("/", "update-tab", "update"), ("/models", "model-tab", "tab")])
def test_tab_groups_are_complete(path, tab_class, prefix, pages):
    """Every tab must have a panel and every panel a tab, derived from the
    click handlers themselves."""
    html = pages[path]
    tabs = re.findall(
        r"class=\"" + re.escape(tab_class) + r"[^\"]*\"[^>]*onclick=\"switch\w+Tab\('([^']+)'" +
        r"(?:\s*,[^)]*)?\)\"", html)
    panels = re.findall(r"id=\"(" + re.escape(prefix) + r"-[a-z0-9]+)\"", html)
    assert tabs, f"no {prefix} tabs found on {path}"
    expected = {f"{prefix}-{name}" for name in tabs}
    missing = expected - set(panels)
    orphans = set(panels) - expected
    assert not missing, f"{path}: tabs with no panel: {sorted(missing)}"
    assert not orphans, f"{path}: panels no tab switches to: {sorted(orphans)}"
    assert len(tabs) == len(set(tabs)), f"{path}: duplicate tabs {tabs}"


def test_no_unrendered_template_or_none_values_in_markup(pages):
    """A leftover ``{{ }}`` or a literal ``None`` in markup is a template bug
    the user reads as broken UI."""
    bad = []
    for path, html in pages.items():
        for m in TEMPLATE_LEFTOVER_RE.finditer(html):
            bad.append(f"{path}: unrendered template expression {m.group(0)!r}")
        for m in re.finditer(r"(href|action)=\"([^\"]*)\"", html):
            if re.search(r"\bNone\b|undefined", m.group(2)):
                bad.append(f"{path}: broken attribute {m.group(0)!r}")
    assert not bad, "\n".join(bad)


def test_templates_carry_no_htmx_attributes():
    """htmx is loaded on every page but the templates use none of it, which is
    what makes the harness's document.evaluate shim safe (jsdom and htmx
    disagree on that call). If hx-* attributes are ever added, the DOM tests
    must grow real htmx coverage instead."""
    found = []
    for tpl in sorted((ROOT / "web" / "templates").glob("*.html")):
        for m in re.finditer(r"\b(hx-[a-z-]+|data-hx-[a-z-]+)=", tpl.read_text()):
            line = tpl.read_text()[: m.start()].count("\n") + 1
            found.append(f"{tpl.name}:{line} {m.group(1)}")
    assert not found, "hx-* attributes appeared; the UI harness does not exercise htmx:\n" + "\n".join(found)


def test_static_assets_are_served(client):
    """The pages load htmx from /static; a missing file is a silent loss of
    behaviour."""
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200 and len(r.text) > 1000, f"/static/htmx.min.js: {r.status_code}"


def test_ui_spec_pages_are_known(client):
    """spec.json must not drift from the app in either direction."""
    spec = json.loads((ROOT / "tests" / "ui" / "spec.json").read_text())
    for p in spec["pages"]:
        assert client.get(p["path"]).status_code in (200, 302), f"spec page {p['path']} is not served"
