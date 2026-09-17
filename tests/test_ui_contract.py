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
# same default the other test modules use: whichever module is imported first
# wins the environment, so a differing password makes test order matter
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

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


# ------------------------------------------------------------------ noise
# The dashboard hero cards are for the two services. Two things crept in that
# were not: a second Metrics link (the nav is right there) and the Vulkan SDK
# path, which is inventory and belongs in the GPU Drivers / SDKs tab.
def test_dashboard_does_not_duplicate_the_navigation(pages):
    html = pages["/"]
    assert html.count('href="/metrics"') == 1, (
        "Metrics is in the top nav; a repeated link under a hero card is noise, "
        "not navigation")


def test_sdk_inventory_lives_in_the_gpu_tab():
    """Asserted on the template, not the rendered page: the ComfyUI log tail is
    part of the page and can legitimately contain the word 'vulkan'."""
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    head, sep, gpu = tpl.partition('id="update-gpu"')
    assert sep, "the GPU Drivers / SDKs panel is gone from the dashboard"
    for word in ("vulkan", "rocm"):
        assert word not in head.lower(), (
            f"{word!r} is marked up outside the GPU Drivers / SDKs tab — the SDK "
            f"inventory has one home")
    assert "gpu-vulkan-sdk" in gpu and "gpu-rocm-version" in gpu


# ------------------------------------------------------- stored build controls
def test_stored_builds_are_one_dropdown(pages):
    """A table of builds grows one row per build forever, and every row did one
    thing: be picked. One dropdown, one count, two actions."""
    html = pages["/"]
    for sel in ('id="build-select"', 'id="build-count"', 'id="switch-build-btn"',
                'id="delete-build-btn"'):
        assert sel in html, f"missing {sel}"
    assert 'id="upd-releases"' not in html, "the old per-build row list is back"
    assert "updRollback(" not in html, "the old per-row rollback handler is still defined"


def test_delete_build_confirms_every_step_of_the_way():
    """An irreversible delete: client confirm carrying the build's details, a
    server that does not believe a request without `confirm: true`, and an audit
    event after it."""
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    body = tpl[tpl.index("async function deleteBuild()"):
               tpl.index("const buildSelectEl")] if "async function deleteBuild()" in tpl else ""
    assert body, "deleteBuild() is missing"
    assert "if (!confirm(" in body, "delete sends the request without confirming first"
    assert "buildFacts(e)" in body, "the confirmation does not show what is being deleted"
    assert "if (e.active)" in body, "the current build is not refused before the request"
    assert body.index("if (!confirm(") < body.index("fetch("), "confirm must precede the request"
    assert "'/api/updates/llamacpp/delete'" in body
    assert "confirm: true" in body

    src = (ROOT / "app" / "main.py").read_text()
    route = src[src.index('"/api/updates/llamacpp/delete"'):]
    route = route[:route.index("\n    @app")]
    assert 'if not body.confirm:' in route, "the endpoint deletes without a confirmation flag"
    assert "db.event(" in route, "the deletion is not audited"


def test_switch_build_locks_the_running_build():
    """The lockout is the safety story of both buttons; assert the JS that
    implements it, not just that a button exists."""
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    sync = tpl[tpl.index("function syncBuildButtons()"):] if "function syncBuildButtons()" in tpl else ""
    assert sync, "syncBuildButtons() is missing"
    sync = sync[:sync.index("\nasync function")]
    assert "sw.disabled = !e || !!e.active;" in sync
    assert "del.disabled = !e || !!e.active;" in sync
    assert "build-hint" in sync, "a locked button should say why it is locked"


# --------------------------------------------------------------------- tabs
TAB_CLASS_RE = re.compile(r"\.(?:update|model)-tabs?[\w-]*(?:\s*,|\s*\{)")


def test_tab_geometry_is_defined_in_one_place():
    """Tab styling used to be copy-pasted into both tab pages and they drifted.
    The component lives in /static/tabs.css; a page re-declaring it can put the
    floating gap back."""
    offenders = []
    for tpl in sorted((ROOT / "web" / "templates").glob("*.html")):
        text = tpl.read_text()
        for m in TAB_CLASS_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{tpl.name}:{line}: {m.group(0).strip()}")
    assert not offenders, (
        "tab geometry declared outside web/static/tabs.css:\n" + "\n".join(offenders))


def test_tabs_stylesheet_is_served_and_well_formed():
    css = (ROOT / "web" / "static" / "tabs.css").read_text()
    assert css.count("{") == css.count("}"), (
        f"unbalanced braces in tabs.css ({css.count('{')} vs {css.count('}')})")


def test_tab_geometry_holds_the_four_rules(client):
    """Cheap mirror of what the DOM harness measures, so a missing file or a
    reverted rule is caught in milliseconds instead of in the browser sweep.

    The contract, in order: no gap under the strip; the strip draws the baseline;
    every tab carries its own bottom edge and overlaps the baseline by 1px; the
    active tab opens that baseline in the panel's colour; and the panel sits
    flush, borderless on top, with square top corners."""
    css = client.get("/static/tabs.css")
    assert css.status_code == 200, f"/static/tabs.css: {css.status_code}"
    assert "text/css" in css.headers.get("content-type", "")
    # comments would otherwise be swept into the selector text and split on
    # their own commas
    sheet = re.sub(r"/\*.*?\*/", "", css.text, flags=re.S)

    def rule_for(selector):
        """The declarations of the rule whose selector list contains `selector`."""
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", sheet):
            if any(part.strip() == selector for part in head.split(",")):
                return body
        return ""

    def margin_bottom(decls):
        """The declared bottom margin, from either the longhand or the shorthand."""
        long = re.search(r"margin-bottom\s*:\s*([^;]+)", decls)
        if long:
            return long.group(1).strip()
        short = re.search(r"margin\s*:\s*([^;]+)", decls)
        if not short:
            return None
        parts = short.group(1).split()
        return parts[{1: 0, 2: 1, 3: 2, 4: 2}[len(parts)]]

    strip = rule_for(".update-tabs") or rule_for(".model-tabs")
    assert strip, "no rule for the tab strip (.update-tabs / .model-tabs)"
    bottom = margin_bottom(strip)
    assert bottom in ("0", "0px"), f"tab strip margin-bottom must be 0, got {bottom!r} in {strip.strip()}"
    assert re.search(r"border-bottom\s*:\s*1px solid var\(--line\)", strip), \
        f"the strip must draw the baseline the tabs stand on: {strip.strip()}"

    tabs = rule_for(".update-tab") or rule_for(".model-tab")
    assert tabs, "no rule for the tabs themselves"
    assert margin_bottom(tabs) == "-1px", \
        f"tabs must sit 1px onto the baseline so their rails reach the panel: {tabs.strip()}"
    assert re.search(r"border\s*:\s*1px solid", tabs), \
        f"each tab needs a full frame, bottom edge included: {tabs.strip()}"

    card = rule_for(".update-tab-content > .card")
    assert card, "no rule joining a tab panel to its card"
    mt = re.search(r"margin-top\s*:\s*([^;]+)", card)
    assert mt and mt.group(1).strip() in ("0", "0px"), \
        f"the panel card must sit flush on the baseline (margin-top:0), got {mt.group(1).strip() if mt else 'no margin-top'}: {card.strip()}"
    assert re.search(r"border-top\s*:\s*none", card), \
        f"panel card must have no top border — the junction would draw twice: {card.strip()}"
    assert re.search(r"border-top-left-radius\s*:\s*0", card), \
        f"panel card needs square top corners where it meets the tabs: {card.strip()}"

    active = rule_for(".update-tab.active")
    assert active, "no rule for the active tab"
    assert re.search(r"background\s*:\s*var\(--panel\)", active), \
        f"active tab must be the panel's own colour, got: {active.strip()}"
    assert re.search(r"border-bottom\s*:\s*1px solid var\(--panel\)", active), \
        f"active tab must open the baseline in the panel colour, got: {active.strip()}"


def test_pages_with_tabs_link_the_stylesheet(pages):
    for path, html in pages.items():
        if 'class="update-tabs"' in html or 'class="model-tabs"' in html:
            assert 'href="/static/tabs.css"' in html, f"{path} has tabs but does not load tabs.css"


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


def test_display_helpers_survive_the_values_the_machine_actually_sends():
    """`_human`/`_dur`/`_ago` are Jinja filters over numbers that come from
    timestamp arithmetic, cgroup counters and a JSON release registry — sources
    that can produce NaN, inf, a string, or a key that is not there at all.

    A display helper that raises is the worst place for it: one odd counter turns
    the whole health page into a 500, which is exactly what these did before the
    _num() guard. And `_human` used to fall out of its unit loop and emit a bare
    str(float) for absurd sizes — 300 digits where a unit belonged.
    """
    from jinja2 import Undefined

    from app.main import _ago, _dur, _human

    for bad in (None, "", "abc", True, float("nan"), float("inf"),
                Undefined(name="total_bytes")):
        assert _human(bad) == "—", bad
        assert _dur(bad) == "—", bad
        assert _ago(bad) == "—", bad

    # 0 is a real measurement, not a missing one, and the three disagree about
    # it on purpose: "0 seconds ago" *is* "just now", while 0 bytes and 0 hours
    # of age have nothing to show.
    assert _dur(0) == "just now"
    assert _human(0) == "—"

    assert _human(512) == "512B"
    assert _human(71 * 1024 ** 3) == "71.0G"
    # Never a bare float: every answer carries a unit, however large.
    huge = _human(1e308)
    assert huge[-1].isalpha(), huge
    assert len(huge) < 24, f"absurd size must not print {len(huge)} digits"
    assert _dur(7200) == "2h 00m"
    assert _ago(3.0) == "3h"


def test_provider_accessors_are_typed_and_do_not_shadow_their_submodules():
    """`providers.comfyui` used to be the accessor's name, which shadowed the
    `app.providers.comfyui` submodule on the package namespace: `from
    app.providers import comfyui` handed back a function and
    `comfyui.ComfyUI` died with an AttributeError at import time. The accessors
    are `get_*` for exactly that reason, and this pins the shape so a later
    rename cannot quietly reintroduce it.
    """
    import importlib

    from app import providers

    for tool in ("llamacpp", "comfyui", "huggingface"):
        # The submodule must remain reachable *as a module* — that is what broke
        # first time: an accessor named `comfyui` replaced it on the package
        # namespace, so `comfyui.ComfyUI` was an AttributeError on a function.
        mod = importlib.import_module(f"app.providers.{tool}")
        assert getattr(providers, tool) is mod, (
            f"providers.{tool} must be the submodule, not an accessor")
        accessor = getattr(providers, f"get_{tool}")
        assert callable(accessor)

    from app.providers.comfyui import ComfyUI
    from app.providers.huggingface import HuggingFace
    from app.providers.llamacpp import LlamaCpp

    assert isinstance(providers.get_comfyui(), ComfyUI)
    assert isinstance(providers.get_llamacpp(), LlamaCpp)
    assert isinstance(providers.get_huggingface(), HuggingFace)
    # get() keeps answering with the base type and None for an unknown key.
    assert providers.get("nope") is None
