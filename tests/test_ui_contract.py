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

# The row module is asserted alongside the template: a contract test that only reads the template
# text can check a loop, not what the loop renders, and the loop's data is the registry's.
from app.core import personality as P  # noqa: E402
from app.core import settingrows  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
# The old /metrics page is on the dashboard now (docs/PORTAL_PLAN.md sec 6.13), so the
# monitored set is the pages that exist, not the pages that once did.
PAGES = {"/": "dashboard", "/models": "models", "/settings": "settings"}
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
        defined = {g for m in FUNC_DEF_RE.finditer(js) for g in m.groups() if g}
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
def test_dashboard_does_not_link_a_page_that_no_longer_exists(pages):
    """The Metrics page was a superset of the dashboard, so it is gone.

    A nav link to a deleted page is a 404 the operator finds by clicking, and the
    audit that removed the page has to remove its links in the same change.
    """
    html = pages["/"]
    assert 'href="/metrics"' not in html, (
        "the dashboard still links /metrics — the merge left a dead link behind")
    # 2026-10-28: the owner retired the Machine detail panel; its live role is
    # the nav's resource lights (tests/test_hero.py pins the pair). The dead
    # /metrics link guard above is the part of this contract that outlived it.
    assert 'id="reslights"' in html, (
        "the machine-detail panel is gone and no resource lights replaced it")


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


def test_the_dashboard_carries_no_unlabeled_static_metrics(pages):
    """The old contract ("rendered-once numbers must print their sample time")
    is satisfied by removing the rendered-once numbers: the machine detail
    panel (disk/GPU cards at page load) was retired by the owner on
    2026-10-28, and every resource number left on the page is polled - the
    nav's lights by a 20 s loop, the heroes by their own. A page may not show
    a fixed number that reads as live; a page showing no such number passes
    by construction."""
    html = pages["/"]
    assert "sampled " not in html, (
        "a rendered-once metric is back on the dashboard without a sample time "
        "- label it, poll it, or take it out")
    assert 'id="reslights"' in html, (
        "the resource lights are the freshness-bearing replacement; they must exist")

# ------------------------------------------------- what the polls are allowed to cost
# Measured on this box (docs/PORTAL_PLAN.md sec 6.13): the router snapshot is 0.6 ms,
# /api/metrics 0.9 ms, the service list 11 ms, the GPU probe 24 ms, the llama.cpp
# release check 92 ms, and the huggingface-cli check 219 ms. Only the first two are
# cheap enough to run every few seconds, and only the panels that show a change over
# time are worth polling at all.
def test_the_dashboard_polls_only_what_moves_and_only_while_you_are_looking():
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    # A background tab must not generate server traffic.
    assert "document.hidden" in tpl, (
        "no visibility guard: an open background tab keeps polling the server")
    assert "setInterval(tick," not in tpl, "the router loop ignores page visibility"
    assert "setInterval(comfyTick," not in tpl, "the ComfyUI loop ignores page visibility"
    # The tab panels are fetched when their tab opens, not paid at page load. The
    # ComfyUI hero card is not a tab panel: it fills from loadComfyUpd() at page load,
    # which costs 3.3 ms and shows a service state the operator watches.
    for call in ("loadPythonInfo();", "loadGpuInfo();", "loadHfUpd();"):
        assert f"\n{call}" not in tpl, (
            f"{call} runs at page load: the tab that shows it is the place to fetch it")
    assert "loadPanelOnce('llamacpp')" in tpl, (
        "the llama.cpp tab is displayed at page load, so its fetch has to happen there")
    # The huggingface-cli check is the most expensive call on the portal (219 ms,
    # measured) and the panel it fills is inventory: button press only.
    assert "setInterval(loadHfUpd" not in tpl, (
        "the huggingface-cli panel has a polling timer; 219 ms a request is a "
        "button press, not a loop")
    assert "hfCheckUpdates" in tpl, (
        "the huggingface-cli panel has no Check button, so its data is unreachable "
        "without a page reload")
    # The machine detail is the panel with a time signal; its poll is gated on the panel.
    # 2026-10-28: the machine detail panel (the one dashboard poll gated on an
    # open panel) was retired by the owner. The dashboard must not poll
    # /api/metrics again; the nav's resource lights poll it globally at a
    # patient 20 s, visibility-guarded like every other loop on the portal.
    assert "machineTick" not in tpl, "the retired machine panel's poll is back"
    base = (ROOT / "web" / "templates" / "base.html").read_text()
    assert "setInterval(run, 20000)" in base, (
        "the resource lights' global poll should stay patient (20 s), not per-second")
    assert "document.hidden" in base, "the global strip must not poll background tabs"

# --------------------------------------------------- locked before it has data
def test_update_buttons_ship_locked_in_the_markup():
    """A control that acts on machine data is disabled in the template, not enabled
    and then disabled by a callback.

    The UI harness also checks button state at page settle, which is a race with the
    panel fetch: a panel that fills faster wins, and the assertion flips for reasons
    that have nothing to do with the page. The template is the place where "locked
    until there is something to act on" is a fact, so it is asserted there.
    """
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    for btn in ("upd-btn", "comfy-upd-btn", "hf-upd-btn", "switch-build-btn",
                "delete-build-btn", "purge-stray-btn",
                "oc-upgrade-btn", "oc-restart-btn", "oc-plugins-btn"):
        tag = next((ln for ln in tpl.splitlines() if f'id="{btn}"' in ln), None)
        assert tag is not None, f"button {btn} is not on the dashboard"
        assert "disabled" in tag, (
            f"{btn}: ships enabled, so it is clickable before the page knows the "
            "machine state; the attribute belongs on the button, not on a callback")

def test_panel_buttons_are_unlocked_by_the_panel_that_fills_them():
    """The lock is released where the data arrives, not somewhere the data can reach
    by luck.

    The dashboard fetches its tab panels the first time each tab opens (§6.13). So the
    code that enables a panel's button has to live in that panel's loader: if it sat in
    a page-load path, the button would unlock for a panel with nothing in it, and a
    fixture that changes the panel's data would change the button's state for reasons
    the page does not control.
    """
    tpl = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    pairs = {"#comfy-upd-btn": "loadComfyUpd", "#hf-upd-btn": "loadHfUpd",
             "#oc-upgrade-btn": "loadOpencodeUpd"}
    for sel, loader in pairs.items():
        start = tpl.index("function " + loader + "(")
        end = next(i for i in range(start + 1, len(tpl)) if tpl[i:i + 9] == "function ")
        body = tpl[start:end]
        btn = sel.lstrip("#")
        assert btn in body, (
            f"{sel} is enabled outside {loader}(), so its lock is not tied to its panel")
        assert re.search(r"getElementById\('" + btn + r"'\)", body), (
            f"{loader}() fills the panel without touching {btn}")


def test_the_reference_links_come_from_the_row_and_not_from_the_template(client):
    """The anchors are row data, so the template names the field, never a key.

    The point of §6.14.1 is that a registry key is a data edit. That survives only while the
    template's link is `{{ r.settings_ref }}`: a template carrying `href="/help#ctx-size"` is a
    page that knows the registry's contents, and a key added tomorrow gets a row with no link.

    §6.14.1q moved the keys table to the settings reference (queue 33, state 4), so the count is the
    two editor grids, not table + grids. The *rule* is unchanged and is the reason the move was
    safe: the anchor is a field on the row, in the page and in the document that renders it.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert tpl.count('href="/help#{{ r.settings_ref }}"') == 2, \
        "the reference link belongs in both editor grids (the keys table moved to the reference)"
    assert not re.search(r'href="/help#[a-z][a-z0-9-]*"', tpl), \
        "the template hardcodes a per-key settings anchor; the anchor is row data"
    assert 'href="/help#{{ r.help_ref }}"' in tpl, "the plan-section link is gone with it"

    # The one page-level anchor (the reference's own heading) is asserted **on the rendered page**:
    # the rule that keeps a registry key a data edit is about per-key anchors, and a named section of
    # the reference is not a key the registry can grow. Asserting it in the template source is what
    # made the whole rule read as a template-file regex.
    from app.core import kb

    page = client.get("/personalities").text
    anchor = kb.anchor_of("Settings a personality can set")
    assert f'href="/help#{anchor}"' in page, "the page does not link the reference's own section"
    assert kb.find(anchor) and kb.find(anchor)["doc"] == "PORTAL_SETTINGS.md", \
        "the page links a heading that is not in the settings reference"

    # The document that now owns the reference builds its links from the same row fields.
    doc = (ROOT / "app" / "core" / "settingsdoc.py").read_text()
    assert "row['help_ref']" in doc, "the reference's evidence link is not row data"


def test_the_editor_grids_render_rows_and_hide_what_a_file_cannot_use():
    """The editor is the row component, and a hidden row cannot reach the payload.

    Two properties of the page source, both of which the rendered page alone would not prove:

    - The grids loop over `editor_rows`. They used to loop over `setting_names` and compute the
      control shape from `SETTINGS` in the template, which is the duplication §6.14.1 exists to
      delete: a registry key needed a template edit, and the template's idea of a key could drift
      from the generator's.
    - The client treats a hidden row as absent. The per-model controls are one set of inputs reused
      across every model, so a value typed for a model that supports a setting is still sitting in
      the box when a model that does not is selected. If capture read hidden rows, the page would
      propose a key the file cannot use; if load left a stale value in one, the same.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    ed = tpl[tpl.index('id="editor"'):]
    assert "setting_names" not in ed, "the editor is looping the registry names again"
    assert ed.count("for r in editor_rows") == 2, \
        "both grids must render rows; one of them is still hand-built"
    assert "if (f.hidden) { delete st[f.dataset.key]; continue; }" in ed, \
        "capture reads hidden rows"   # §6.14.1i: a hidden row is also dropped from the working copy
    assert "control.value = f.hidden ? '' : (saved[f.dataset.key] || '');" in ed, \
        "loadModel leaves a stale value in a row the model cannot use"
    assert "applyVisibility(name);" in ed, "visibility is computed but never applied on load"
    # The reason text is what an operator reads; a row that vanishes silently is the failure.
    assert "when.textContent = why;" in ed, "the row says nothing about why it is hidden"


# ------------------------------------- the row's shape, as a claim about the page source
#
# The operator's notes on the shipped row component (§6.14.1, slice 2): the rows read "thick" --
# the default and the help line each took a line of their own -- and the fields looked like a
# browser default field on a dark page. Both are layout claims, and layout claims are testable
# against the source: the markup order and the CSS rules together say what a line contains.


def test_the_help_line_is_the_control_you_click():
    """The short description is the disclosure control, with the toggle word at its end.

    The old row had the help sentence as its own line and a `<summary>detail</summary>` under it --
    two lines per row, one of them saying nothing, and the clickable word in a third place from the
    sentence it opened. Now the sentence itself opens the detail, so the thing you read is the thing
    you click.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    ed = tpl[tpl.index('id="editor"'):]
    for grid in ("ed-global", "ed-model-fields"):
        block = ed[ed.index(f'id="{grid}"'):]  # from this grid to the end of the editor
        assert '<summary><span class="mut h">{{ r.help }}</span>' in block, \
            f"{grid}: the help sentence is not inside the summary -- the row has a line for it again"
        assert '<span class="ed-more ed-more-closed">more</span>' in block, \
            f"{grid}: the disclosure control has no toggle word at the end of the sentence"
        assert '<span class="ed-more ed-more-open">less</span>' in block, \
            f"{grid}: an open detail does not say that clicking again closes it"
    # The word is CSS state, not script state: nothing has to run for "more"/"less" to be true.
    assert '.ed-detail[open]>summary .ed-more-closed{display:none}' in tpl
    assert '.ed-detail:not([open])>summary .ed-more-open{display:none}' in tpl


def test_the_help_outline_is_a_list_that_scrolls():
    """A 253-section outline is a vertical list, never a column ribbon.

    docs/PORTAL_POLISH.md §9 measured the help page's TOC at 1,024 px of client width against
    13,749 px of content -- 13.4 viewports of horizontal scrolling, 208 nodes in one ribbon.
    The cause is the CSS multi-column layout (`columns:2`): columns balance content ACROSS
    the inline axis, so a long vertical list becomes a wide one that the box's own
    `overflow-y:auto` cannot reach. The reader scans a table of contents top to bottom.

    Oct-26 the owner asked for a two-pane docs browser: the class carrying the outline is
    now `.kb-outline` (the rail), and the ban travels with it -- the rail is a vertical
    scrolling list, not a ribbon, and not a width-capped box that hides sections below
    the fold.
    """
    tpl = (ROOT / "web" / "templates" / "help.html").read_text()
    head = tpl[:tpl.index("</style>")] if "</style>" in tpl else tpl
    rules = re.findall(r"\.kb-outline\s*\{([^}]*)\}", head, re.S)
    assert rules, "no .kb-outline rule in the help page's own stylesheet"
    decls = ";".join(rules)
    for prop in ("column-count", "columns"):
        m = re.search(rf"(^|[;\s]){prop}\s*:\s*([^;]+)", decls)
        assert not m or m.group(2).strip() in ("1", "1 auto", "auto 1"), (
            f".kb-outline declares {prop}: {m.group(2).strip()!r}. A multi-column TOC is a ribbon: "
            "columns balance the list across the inline axis, so 253 sections become 13.4 "
            "viewports of horizontal scrolling (measured: 1,024 px client width, 13,749 px "
            "content, docs/PORTAL_POLISH.md §9). The outline is a vertical list.")
    assert "overflow-y:auto" in decls or "overflow:auto" in decls, (
        f".kb-outline has no vertical scroll in {decls.strip()!r}: a scrolling rail that "
        "cannot scroll hides the sections below the fold, which is the failure the ribbon "
        "was a workaround for")


def test_the_help_outline_is_an_outline_of_documents():
    """A table of contents groups by document, inside a reading order.

    docs/PORTAL_PLAN.md §6.14.2: the outline lists the living documents and their
    headings -- a flat ribbon of links does not answer "which document is §6.14.1u in".
    Oct-26 the owner added the layer above it: friendly docs first, the engineering
    corpus grouped under Reference & history, "visible if selected like any other
    link" -- so the rail renders kb.outline()'s groups, a <details> per group and per
    document, and the payload is the group tree, not a flat list.
    """
    tpl = (ROOT / "web" / "templates" / "help.html").read_text()
    rail = tpl[tpl.index('id="kb-outline"'):tpl.index('class="kb-rail-foot"')]
    assert "for g in groups" in rail, "the rail does not render the outline's groups"
    assert "kb-group" in rail and "kb-toc-doc" in rail, (
        "the outline is not a two-level tree: groups of documents, documents of sections")
    assert "data-doc" in rail, "document groups do not name the document they carry"
    main = (ROOT / "app" / "main.py").read_text()
    assert '"groups": kb.outline()' in main, "the payload must carry the group tree"
    kbmod = (ROOT / "app" / "core" / "kb.py").read_text()
    assert '"Reference & history"' in kbmod, "the engineering corpus has no group"
    assert re.search(r'"Reference & history",\s*\[[^\]]*\],\s*False', kbmod), (
        "the Reference & history group is open by default: the owner's direction is the "
        "engineering corpus collapsed, not hidden -- visible if selected, like any other link")


def test_the_help_page_is_a_two_pane_browser():
    """The owner's layout (Oct-26): narrow outline rail left, document viewer right,
    search impossible to miss, and the section fetch's sanitizer untouched.

    The pin is on the declarations: the grid rule, the rail/viewer ids, the search
    form sitting above the outline in the rail, the friendly anchors the welcome
    card offers resolving in the corpus, and the sanitizer allowlist surviving the
    rewrite (the section body is fetched doc text; the allowlist is the XSS gate).
    """
    tpl = (ROOT / "web" / "templates" / "help.html").read_text()
    head = tpl[:tpl.index("</style>")]
    assert re.search(r"\.kb-app\s*\{[^}]*grid-template-columns:\s*300px", head), (
        "no two-pane grid (rail + viewer) in the help page")
    for id_ in ("kb-rail", "kb-viewer", "kb-outline", "kb-search", "kb-welcome",
                "kb-results", "kb-section", "kb-rail-toggle"):
        assert f'id="{id_}"' in tpl, f"missing {id_}"
    assert tpl.index("kb-searchbar") < tpl.index('id="kb-outline"'), (
        "the search box is not at the top of the rail: the owner asked for easy to find")
    assert 'aria-expanded' in tpl, "the rail collapse has no aria state"
    assert "const ALLOWED" in tpl and "SAFE_HREF" in tpl, (
        "the section renderer's node allowlist is gone -- fetched doc text must stay sanitized")
    from app.core import kb
    ids = {s["id"] for s in kb.sections()}
    for anchor in ("1", "words-you-ll-meet", "gguf", "quantization", "context-window",
                   "kv-cache", "mmproj", "slot", "personality", "router", "harness", "mcp"):
        assert anchor in ids, f"the welcome card links /help#{anchor}, which names nothing"
    assert kb.find("words-you-ll-meet")["doc"] == "GLOSSARY.md"
    assert kb.find("gguf")["doc"] == "GLOSSARY.md"


def test_a_row_is_one_line():
    """Name, control and default share the line; the default sits right of the control.

    The row component is a grid whose rules place each part. `grid-column:2` on `.ed-default` is the
    two-line row: the default under the control, in its own band. `flex:0 0 auto` in a baseline-aligned
    flex row is the one-line row. The assertion is the CSS, because the markup order alone would pass
    while a grid rule stacked the parts again.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    css = tpl[:tpl.index("</style>")]
    assert ".ed-field{display:flex" in css, "the row is not a flex line"
    assert "align-items:baseline" in css, "the parts of a row are not aligned on the text baseline"
    assert ".ed-default{flex:0 0 auto" in css, "the default is not on the control's line"
    assert ".ed-default{grid-column" not in css, "the default has its own grid band again"
    assert ".ed-detail{flex:1 1" in css, "the help line does not take the rest of the row's line"
    ed = tpl[tpl.index('id="editor"'):]
    for grid, control in (("ed-global", 'placeholder="leave as is"'), ("ed-model-fields", 'placeholder="unset"')):
        block = ed[ed.index(f'id="{grid}"'):]
        assert block.index(control) < block.index('<span class="ed-default mut">'), \
            f"{grid}: the default is not to the right of the control"
        assert block.index('<span class="ed-default mut">') < block.index('<details class="ed-detail">'), \
            f"{grid}: the default is not on the line the control is on"


def test_the_form_controls_wear_the_page_theme():
    """Every page is dark from the canvas up; a field must not be the one white box on it.

    Scoped site-wide in base.html, not per page: a field on /settings, /models or /help is the same
    object, and a theme applied one page at a time is a theme that leaks. `color-scheme:dark` is the
    part that reaches the parts CSS cannot paint -- the native dropdown list and the date picker.
    """
    base = (ROOT / "web" / "templates" / "base.html").read_text()
    assert "input:not([type=button]):not([type=submit]):not([type=image]),select,textarea{" in base, \
        "text fields, selects and textareas are not styled as a set"
    assert "background:var(--field);color:var(--fg);border:1.5px solid var(--field-line)" in base, \
        "controls are painted with values from outside the page's own palette (Oct-18: the " \
        "--panel-on--panel 1px rule is what made fields invisible; --field/--field-line are " \
        "the same palette's control layer)"
    assert "select>option{background:var(--field);color:var(--fg)}" in base, \
        "the field is themed and its dropdown is not"
    assert "color-scheme:dark" in base, "native popups (dropdown, picker) keep the light palette"
    assert "input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}" in base, \
        "focus is not visible in the page's own accent"


# ------------------------------------------------- the deck's one verb (§6.14.1f)
#
# The operator reads this page while a router is running. The deck is the thing they click to
# find a personality among ten, so it must be structurally incapable of changing the router --
# not "we hope the click handler is small", but a claim about the source of the block that
# handles it.

def _deck_script(tpl: str) -> str:
    """The deck's own <script> block: the one that names pers-deck."""
    blocks = re.findall(r"<script>(.*?)</script>", tpl, re.S)
    decks = [b for b in blocks if "pers-deck" in b]
    assert len(decks) == 1, f"expected exactly one script block for the deck, got {len(decks)}"
    return decks[0]


def test_raising_a_card_is_reading_and_not_applying():
    """The deck's script cannot write, and the raised state is an attribute, not a style."""
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    deck = _deck_script(tpl)
    for forbidden in ("fetch(", "XMLHttpRequest", ".submit(", "innerHTML", "location.href",
                      "localStorage"):
        assert forbidden not in deck, f"the deck's script contains {forbidden}: raising would write"
    # The strip is a button, so the deck works with no script at all; raised state is data on the
    # card, so nothing that sets styles for other reasons can unhide a body.
    assert 'class="pers-strip" data-raises="{{ c.name }}"' in tpl
    assert 'data-raised="{% if c.active %}yes{% else %}no{% endif %}"' in tpl
    assert '.pers-card .pers-body{display:none}' in tpl
    assert '.pers-card[data-raised="yes"] .pers-body{display:block}' in tpl
    assert 'style="display' not in tpl.split('<div class="pers" id="pers-deck">')[1]


# ------------------------------------------------- the switch's words (§6.14.1g)
#
# A boolean row is the only control whose values the page invents, and inventing them is how the
# portal ended up offering `1`/`0` to a validator that refused every string. The values are the
# row's now; these are the source-level claims that keep them there.

def test_a_switch_builds_its_options_from_the_row():
    """Both grids loop over `r.accepted`, and neither hardcodes a pair.

    The claim is about the source because the page's options are the row's: a template that
    spelled `on`/`off` by hand would keep passing every browser check while a second switch key
    joined the registry with words nothing reads. The browser case in tests/ui/spec.json
    (`#ed-g-jinja-sel`, value `on`) is what proves the loop renders.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert tpl.count("{% for v in r.accepted %}<option value=\"{{ v }}\">") == 2, \
        "the switch options are not built from the row in both grids"
    assert '<option value="1">on</option>' not in tpl, \
        "a hardcoded on/off pair is back: the control would offer words the registry refuses"
    assert "<option value=\"yes\">" not in tpl and "<option value=\"no\">" not in tpl, \
        "yes/no are not spellings is_truthy knows"


def test_the_page_and_the_validator_share_one_list_of_boolean_words():
    """Whatever the template loops over is checked against the runtime's vocabulary at import.

    `settingrows.accepted_of` refuses a key that advertises a word `is_truthy` does not read, so
    the page cannot offer it; this is the assertion that the switch rows in fact carry the eight
    words and that a non-boolean row carries none (a `choices` row builds its control from
    `choices`, and an `accepted` list on it would be a second, weaker source).
    """
    rows = settingrows.rows()
    switches = [r for r in rows if r["kind"] == "switch"]
    assert switches, "no switch rows: the registry lost every boolean key"
    for r in switches:
        assert set(r["accepted"]) == set(P.TRUTHY) | set(P.FALSEY), (r["key"], r["accepted"])
    for r in rows:
        if r["kind"] != "switch":
            assert r["accepted"] == [], (r["key"], r["accepted"])


# ---------------------------------------------- the editor follows the personality (§6.14.1i)

def _editor_script(tpl: str) -> str:
    """The editor's own <script> block: the one that names ed-persona."""
    blocks = re.findall(r"<script>(.*?)</script>", tpl, re.S)
    edits = [b for b in blocks if "ed-persona" in b]
    assert len(edits) == 1, f"expected one script block for the editor, got {len(edits)}"
    return edits[0]


def _script_of(tpl: str, page: str) -> str:
    """The page's own script — the largest inline block.

    A page may carry a small markup block and the script that runs it; picking the biggest one is
    how this file reads "the page's JavaScript" without deciding per page which index that is.
    """
    blocks = [b for b in SCRIPT_RE.findall(tpl) if b.strip()]
    assert blocks, f"{page} has no inline script, so there is no page behaviour to check"
    return max(blocks, key=len)


def _fn(script: str, name: str) -> str:
    """One function's source, by brace counting from its declaration."""
    start = script.index(f"function {name}(")
    depth, i = 0, script.index("{", start)
    for j in range(i, len(script)):
        if script[j] == "{":
            depth += 1
        elif script[j] == "}":
            depth -= 1
            if depth == 0:
                return script[start:j + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


def test_the_editor_reads_the_personality_it_shows_from_the_page():
    """The values come from the server's view, not from anything the script infers.

    Two halves, both checked in the source: the template puts the view on the option
    (`data-config`, built by `personality.settings_view()`), and the script parses that attribute
    instead of computing a layering of its own. A browser-side copy of the layering is a second
    ruler for the same file -- the failure this page keeps re-meeting (§6.14.1a, §6.14.1i).
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    assert 'data-config=\'{{ config_views.get(c.name, {}) | tojson }}\'' in tpl, \
        "the personality option no longer carries its current settings"
    script = _editor_script(tpl)
    assert "dataset.config" in script, "the script does not read the view off the option"
    assert "JSON.parse" in script, "the view is not parsed as data"
    for invented in ("GLOBAL_DEFAULTS", "SERVER_DEFAULTS", "visible_when", "pooling_type"):
        assert invented not in script, f"the script derives {invented} -- that is the server's ruler"


def test_the_summary_is_a_diff_against_the_current_settings():
    """`proposed` has to mean *different from what the personality carries*.

    Before the fields were seeded, "proposed" meant "non-empty", which was the same claim. With
    prefilled fields it is a comparison, so the comparison is asserted: both grids are diffed
    against the base view, and the payload is built from the working copy -- not by reading the
    inputs at submit time, which is how a hidden row's stale value reaches a file.
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    script = _editor_script(tpl)
    for fn in ("workFor", "baseFor", "captureGlobal", "captureCurrent", "loadPersona"):
        assert f"function {fn}(" in script, f"the editor has no {fn}() -- there is no working copy"
    summary = _fn(script, "summarize")
    assert "changed(st.global, b.global)" in summary, "the global grid is not diffed against the view"
    assert "changed(body," in summary, "the model sections are not diffed against the view"
    payload = _fn(script, "payload")
    assert "querySelector" not in payload, "payload() walks the DOM at submit time"
    # The one field payload() may read is the personality's *name*, which lives in the select. Any
    # other `.value` is a settings control, and a control is the thing the working copy exists to
    # keep out of the payload: hidden rows and cleared rows have values sitting in them.
    reads = re.findall(r"\$\('[^']+'\)\.value", payload)
    assert reads == [], f"payload() reads control values from the DOM: {reads}"
    assert "shown" in payload, "payload() does not say which record it is sending"
    visibility = _fn(script, "applyVisibility")
    assert "delete st[" in visibility, "a row the file cannot use stays a value the page can send"


def test_a_switch_saves_the_fields_it_is_leaving():
    """Both grids reuse one set of controls, so a switch is a save plus a load.

    This is the bug the page actually had: by the time a `change` handler runs, the select already
    holds the *new* name, so capturing "what is in the fields" into the record the select names
    writes the outgoing configuration over the incoming one -- and the fields you just loaded are
    emptied by the values you just left. The assertion is the order, because the symptom is a page
    that looks empty next to a summary that says nothing changed.
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    script = _editor_script(tpl)
    for fn, capture, move in (("loadModel", "captureCurrent()", "captured = name"),
                             ("loadPersona", "captureGlobal()", "shown =")):
        body = _fn(script, fn)
        assert capture in body, f"{fn}() does not capture the fields it is replacing"
        assert body.index(capture) < body.index(move), \
            f"{fn}() changes which record the fields belong to before saving them"
    # Which record is showing is a variable, not a question to ask the selects -- asking the select
    # is the bug above, restated.
    for fn in ("captureGlobals", "captureCurrent", "payload", "summarize"):
        body = _fn(script, fn)
        assert "$('ed-persona').value" not in body, f"{fn}() asks the select which record is showing"


def test_the_personality_select_updates_the_editor():
    """Selecting a personality is a load, not a summary.

    The operator's complaint verbatim: choosing a personality did not update the settings shown.
    Asserted structurally, because the symptom is exactly what a `change` handler that only
    recomputes a sentence looks like -- the page keeps working and shows the wrong configuration.
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    script = _editor_script(tpl)
    # Asserted as a shape, not as one exact line: step 1 added a second load to the same handler
    # (the settings and the identity arrive together), and a literal match on the wiring would fail
    # a handler that is correct and simply says more.
    # Non-greedy up to the first `);` stops at the arrow function's own `);` inside the body, so the
    # wiring is taken to the handler's closing brace instead.
    wiring = re.search(r"\$\('ed-persona'\)\.addEventListener\('change',(.*)", script)
    assert wiring and "loadPersona" in wiring.group(1), \
        "the personality select does not reload the fields"
    assert wiring and "loadIdentity" in wiring.group(1), \
        "the personality select does not load step 1 together with the settings"
    assert wiring and wiring.group(1).index("loadPersona") < wiring.group(1).index("loadIdentity"), \
        "step 1 loads before the fields it must capture have been saved"
    assert "loadPersona();" in script, "the fields are not seeded on page load"


def test_the_editor_creates_saves_and_deletes_a_personality():
    """Step 1's verbs exist, and each one is wired to the endpoint that answers it.

    Create, Save and Delete are the page's oldest missing verbs: the endpoints have validated
    personality writes since phase N4, and the page offered Preview and Apply. Asserted in the page's
    own source because the failure this guards is silent in the same way a dead Apply is -- a button
    that posts to the wrong route, or that posts without asking, looks exactly like one that works.
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    script = _editor_script(tpl)
    for bid in ("ed-create", "ed-save", "ed-delete", "ed-new-name", "ed-models-max",
                "ed-note", "ed-attach", "ed-identity-note"):
        assert f'id="{bid}"' in tpl, f"step 1 has no {bid}"
    body = _fn(script, "identityBody")
    assert "'/api/personalities/save'" in script, "no verb reaches the personality write endpoint"
    assert "'/api/personalities/delete'" in script, "no verb reaches the personality delete endpoint"
    # Delete asks. The endpoint requires confirm=true, so a page that posted without asking would
    # delete on a stray click and report it as a success.
    wired = script[script.index("$('ed-delete').addEventListener"):]
    assert "window.confirm(" in wired.split("writeIdentity")[0], \
        "the delete button posts without asking"
    assert "confirm: true" in wired, "the delete request does not carry the confirm the endpoint wants"
    # Create is gated on a usable name, and the built-in pair is locked by the server's own fact
    # rather than by a second rule invented next to the buttons.
    sync = _fn(script, "syncIdentity")
    assert "ed-new-name').value.trim()" in sync, "create is not gated on a name"
    assert "b.editable" in sync, "the lock on a built-in personality is its own rule, not the view's"
    # The empty model list is the whole library, which is a claim about the personality, not an
    # accident of the form.
    assert "models: checked.length ? checked : null" in body, \
        "an empty model list is sent without saying what it means"
    attach = _fn(script, "syncIdentity")
    assert "whole library" in attach, "the page never says what an unchecked list means"
    # Step 1 touches the personality file only. If it ever reached presets.ini, Preview/Apply would
    # stop being the only way to change the router.
    assert "presets.ini" in _fn(script, "syncIdentity"), "step 1 does not say what it does not write"
    for fn in ("identityBody", "writeIdentity"):
        assert "/api/personalities/apply" not in _fn(script, fn), \
            f"{fn}() reaches the route that restarts the router"


# The tokens as the page writes them: `c.querySelector('.mp-save').addEventListener(...)`, so the
# closing paren belongs to the needle.
CARD_BUTTONS = ["'.mp-strip')", "'.mp-attach')", "'.mp-new')", "'.mp-create')", "'.mp-save')",
                "'.mp-delete')", "'.mp-detach')", "'.mp-remove')"]


def test_the_model_personality_cards_edit_one_object_at_a_time():
    """Step 3 is a deck of shared objects, and the page keeps that a single fact (§6.14.1k).

    Four things are asserted here because each is a way a nested editor goes quietly wrong: two
    copies of the per-model grid (so two cards can show two values for one key), a card that works out
    `used by` for itself (the delete endpoint refuses on the strength of that answer), a strip that
    reads the model select instead of moving it (the sweep then drives one model while the open card
    names another), and a detach that fires a request (it is the personality's choice, and the
    personality is written by step 1's Save).
    """
    tpl = (ROOT / "web/templates/personalities.html").read_text(encoding="utf-8")
    script = _editor_script(tpl)
    assert tpl.count('id="ed-model-fields"') == 1, \
        "the per-model grid exists more than once, so two cards can disagree about one key"
    assert "$('.mp-slot').appendChild" not in script and \
        "c.querySelector('.mp-slot').appendChild($('ed-model-fields'))" in script, \
        "the open card does not host the one grid"
    assert "CONFIG_USES" in script and "dataset.configs" in script, \
        "the shared objects are not read from the data the server authored"
    assert "assigned_personalities" not in script

    # The two selectors are arrow consts, not `function` declarations, so they are sliced by their
    # own terminator rather than by _fn's brace counting.
    strip = script[script.index("const selectModel ="):script.index("};", script.index("const selectModel ="))]
    assert "dispatchEvent(new Event('change'))" in strip, \
        "clicking a card reads the model selector without moving it"
    close = script[script.index("const closeCard ="):script.index("};", script.index("const closeCard ="))]
    assert "showCard(null)" in close, "a second click on the open card does not answer"

    attach = _fn(script, "attachConfigToPersonality")
    assert "attachBoxes()" in attach and "syncIdentity()" in attach, \
        "the card's model personality is a second list, not the one step 1 saves"
    def card_handler(token: str) -> str:
        """One card button's listener, sliced from its own addEventListener to the next control."""
        start = script.index(f"{token}.addEventListener")
        ends = [script.index(nxt + ".addEventListener", start + 1) for nxt in CARD_BUTTONS
                if script.find(nxt + ".addEventListener", start + 1) > start]
        ends.append(script.index("$('ed-preview').addEventListener"))
        return script[start:min(ends)]

    detach = card_handler("'.mp-detach')")
    assert "postConfig" not in detach and "fetch(" not in detach, \
        "detach posts: detaching is the personality's choice, and Save writes it"

    create = card_handler("'.mp-create')")
    save = card_handler("'.mp-save')")
    drop = card_handler("'.mp-delete')")
    assert "/api/model-configs/save" in create and "/api/model-configs/save" in save, \
        "the card's verbs do not reach the model personality endpoint"
    assert "/api/model-configs/delete" in drop and "window.confirm(" in drop, \
        "delete is not confirm-gated"
    assert "/api/personalities/apply" not in create + save + drop, \
        "a card verb reaches the route that restarts the router"


def test_the_dashboard_reports_what_the_router_actually_is():
    """The process census (§6.14.1l) exists, is read on demand, and gates the one verb it informs.

    A unit reporting `active` is not the fact the operator needs: a `llama-server` started by hand on
    the router's port means `systemctl start` succeeds and every client is talking to the other
    process. So the panel is asserted to say three things the unit state cannot say — which process is
    the router, which processes are nobody's, and which preset file the running process was given —
    and the start button is asserted to consult them, because that is the moment the information has
    a decision attached to it.
    """
    tpl = (ROOT / "web/templates/dashboard.html").read_text(encoding="utf-8")
    for eid in ("router-census", "census-state", "census-workers", "census-foreign",
                "census-clash", "census-presets", "census-served", "census-refresh",
                "census-when"):
        assert f'id="{eid}"' in tpl, f"the census panel has no {eid}"
    assert "it is reporting that it did not look" in tpl, \
        "an unreadable process list is allowed to read as 'nothing outside the unit'"
    assert "the running router reads" in tpl, "the panel never compares the file the router loaded"

    svc = _fn(tpl, "svcAction")
    assert "CENSUS" in svc and "foreign" in svc, \
        "start does not consult the census: the panel is decoration, not a gate"
    start_block = svc[svc.index("action === 'start'"):svc.index("action !== 'start'")]
    assert "confirm(" in start_block, "the census names the stray processes without asking anything"
    assert "port" in start_block, "the port clash is not part of the decision it informs"

    refresh = _fn(tpl, "censusRefresh")
    assert "/api/router/report" in refresh, "the refresh button does not re-read the census"
    render = _fn(tpl, "renderCensus")
    assert "innerHTML" not in render and "innerHTML" not in svc, "the census renders unescaped markup"

    # Read on demand, not on the pulse: a `ps` and an HTTP probe are not a 2.5-second number, and a
    # panel that cost the page a poll would be quietly slowing down the part that matters.
    # The pulse's own poll: `tick`, from its declaration to the interval it schedules itself with.
    poll = tpl[tpl.index("async function tick("):tpl.index("const _poll =")]
    assert "router/report" not in poll, "the census rides the router's poll"

# ------------------------------------------------------------- the projects page (§6.14.1n)
# The page shows a shortlist of checkboxes, a plan, and a registry. Its promises are all
# statements about the server: that a missing toolchain is refused by name, that a plan creates
# nothing, that forgetting deletes nothing, that Launch is the harness page's verb. All of them are
# checkable in the page's own JavaScript — which is exactly the point: a page that says one thing
# and posts another is the failure this test exists for.
def test_the_projects_page_hands_the_operator_to_the_real_opencode_web():
    """Owner Oct-18: the portal's own chat panel is a viewer, not the harness -- "I was
    hoping opencode had a web interface ... and that you could just serve a real opencode
    session from this server via the portal." `opencode serve` ships the browser client
    at its root; the page must hand off to it (per row) and name where the sign-in
    secret lives WITHOUT putting the secret in the page. And the store is shared with
    the operator's manual `opencode` (the isolation's v1 reason is retired), so sessions
    launched here appear in `opencode` started from the project folder.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    main = (ROOT / "app" / "main.py").read_text()
    hc = (ROOT / "app" / "core" / "harnessctl.py").read_text()
    assert "'a', 'proj-web'" in tpl and "Open in OpenCode" in tpl, "no handoff to the real web UI"
    assert "STATE.harness_web" in tpl and "target = '_blank'" in tpl, "the handoff is not a new-tab link"
    assert '"harness_web": _link(' in main, \
        "the payload must carry the host's web root, spelled with the CLIENT'S host"
    assert '"harness_key": str(harnessctl.key_path())' in main, \
        "the page names the KEY FILE; it never carries the password"
    assert "read_key()" not in main, "the portal must not read the harness password into a page"
    assert '"XDG_DATA_HOME"' not in hc, \
        "one shared v2 store: a pinned data home hides portal sessions from manual opencode"
    assert "err_4c90c294" in hc, "the v1-isolation history stays stated so nobody re-isolates blindly"
    assert "closed" in hc and '"stale"' in hc, \
        "a closed turn's running tools read as stale, not as hours of fake activity"


def test_browser_jump_off_links_echo_the_clients_host():
    """Owner Oct-26: every link the portal serves must be spelled with an
    address the clicking system can reach. The rule: links the BROWSER follows
    carry the host the browser itself used (`_link`/request-host); URLs that
    live services on THIS box (agent configs, harness baseUrls, probes) stay
    loopback. Pinned: the projects payload builds harness_web via _link, and
    the dashboard ships no literal loopback jump-off href."""
    main = (ROOT / "app" / "main.py").read_text()
    dash = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    assert "_link(request,\n                                    serviceconf.port_of(\"harness\"" in main \
        or '"harness_web": _link(' in main
    assert 'href="http://127.0.0.1' not in dash, \
        "a literal loopback jump-off link in the dashboard markup: a LAN browser \
follows it to itself"
    assert "router_web" in dash and "comfy.link" in dash, \
        "the hero links must use the request-host builders, not the payload's loopback view"


def test_the_opencode_tab_names_the_three_versions_and_keeps_the_key_out():
    """Owner Oct-25: a Status tab for the harness's updates. The page must show
    the three versions (they are three facts: running / disk / latest-v2), the
    verbs must confirm, and the harness's Basic-auth password must never reach
    any page's markup — the pages name the key FILE, the server reads the key."""
    dash = (ROOT / "web" / "templates" / "dashboard.html").read_text()
    settings = (ROOT / "web" / "templates" / "settings.html").read_text()
    for marker in ("switchUpdateTab('opencode', this)", 'id="update-opencode"',
                   "oc-running", "oc-disk", "oc-latest", "oc-plugins",
                   "loadOpencodeUpd", "ocVerb"):
        assert marker in dash, f"opencode tab marker missing: {marker}"
    for verb in ("upgrade", "restart", "plugins"):
        assert f"'{verb}'" in dash, f"verb button for {verb} missing"
    assert "window.confirm(confirmText)" in dash, "the verbs fire without a confirmation"
    assert "if (OC_BUSY) return;" in dash, "a verb mid-flight must not be clickable twice"
    for page, name in ((dash, "dashboard"), (settings, "settings")):
        assert "OPENCODE_SERVER_PASSWORD" not in page, \
            f"{name}: the harness password env var has no business in page markup"
    # Settings rows are payload-driven; the harness row's constraint (modes
    # loopback|all) must come from the endpoint, not be hardcoded in the page.
    assert "s.modes" in settings, "the bind select ignores the payload's modes"
    assert "'harness'" in settings, "no harness row in the service order"


def test_the_projects_page_shows_the_toolchain_it_measured():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")

    assert "toolchains" in tpl, "the page renders no toolchain census, so it is assuming toolchains"
    assert "TOOLCHAINS" in script
    step_row = script[script.index("function renderSteps"):script.index("const ticked")]
    assert "missing" in step_row and "unavailable" in step_row, \
        "a box whose toolchain is absent is drawn exactly like one that can run"
    assert "hint" in step_row, \
        "the box says a tool is missing without the install statement the server supplied"
    # The census has to be consulted *in the sentence that decides availability*: a page that
    # computes `missing = []` and keeps every other word of this function intact offers every box.
    census_line = [ln for ln in step_row.splitlines() if "const missing" in ln]
    assert census_line, "the availability decision is not one readable line"
    assert "spec.needs" in census_line[0] and "TOOLCHAINS" in census_line[0], \
        f"availability is not decided from the box's needs against the measured census: {census_line[0].strip()}"
    assert "STEPS[id]" in step_row, "the box list is written in the page instead of read from the shortlist"


def test_the_projects_page_shows_the_plan_it_asked_for():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")
    plan = _fn(script, "plan")

    assert "/api/projects/plan" in plan, "the Plan button does not ask the plan endpoint"
    assert "'POST'" in plan, "the plan is asked for as a GET, so it is a page read, not a plan"
    assert "refused" in plan and "reason" in plan, \
        "the plan output drops the refusals: the operator sees a plan with holes in it"
    assert "run" in plan, "the plan output never lists what will run"


def test_the_projects_page_creates_only_through_the_endpoint_that_reports_a_half_built_project():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")
    create = script[script.index("$('#proj-create').addEventListener"):]
    create = create[:create.index("$('#proj-rows').addEventListener")]

    assert "'/api/projects'" in create, "Create does not post to the create endpoint"
    assert "steps: ticked()" in create, "Create posts without the boxes the operator ticked"
    assert "detail" in create, "a refusal from create is swallowed: the button appears to do nothing"
    assert "warning" in create, \
        "a half-built project is shown as success: the page hides the failing step the endpoint named"
    assert "refused" in create, "the init refusals are not reported after a create"


def test_the_projects_page_asks_before_it_forgets_and_says_what_forgetting_does_not_do():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")
    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]

    assert "confirm(" in rows, "Forget is a one-click write"
    assert "not touched" in rows or "never deletes" in rows, \
        "the confirm does not say that the folder on disk is left alone"
    assert "confirm: true" in rows, "Forget posts without the confirm the endpoint requires"
    assert "/api/projects/forget" in rows, "Forget is not wired to the endpoint"


def test_the_launch_button_is_disabled_by_the_hosts_answer_not_by_the_pages_opinion():
    """§6.14.1m is the harness page's session view; §6.14.1n / queue 33 gave the projects page a
    launch verb of its own. So Launch is no longer an inert label — it acts. What has to stay true
    is the *reason* it is disabled: the host's answer, read off the payload.

    A button that is disabled by a literal is a button that lies once the host starts; a button
    disabled by the payload's `launch.available` flips when the host does, which is the fact the
    operator needs (D1).
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")
    # Asserted on the line that *builds the button*. `data-inert="yes"` also appears in the page's
    # CSS, where it styles the control — an assertion satisfied by a stylesheet proves nothing about
    # the control the page renders.
    rows = script[script.index("function renderRows"):]
    rows = rows[:rows.index("function renderUnadopted")]
    build = rows[rows.index("const launch = el("):]
    build = build[:build.index("const forget = el(")]
    assert "launch.disabled" in build, "the Launch control's state is not set where the button is built"
    assert "available" in build, "Launch is disabled by the page's opinion, not the host's answer"

    rows2 = script[script.index("$('#proj-rows').addEventListener"):]
    rows2 = rows2[:rows2.index("$('#proj-unadopted').addEventListener")]
    launch = rows2[rows2.index("dataset.launch"):]
    launch = launch[:launch.index("dataset.forget")]
    assert "req(" in launch, "Launch is a control that does not act"
    assert "harness" not in launch.lower(), \
        "Launch tells the operator to go to the harness page instead of launching"


def test_the_projects_page_reports_the_refusal_of_every_row_verb():
    """Adopt and Forget refuse things for reasons the operator can act on — the folder is outside
    the projects root, the name is not a project name, the confirm was missing.

    The UI sweep cannot see this: its mutation block means the server never answers those posts, so
    the refusal branch never runs under jsdom. Asserted in the source for that reason.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")

    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]
    assert "refused: ${d.detail}" in rows, "a refused Forget is silent"

    adopt = script[script.index("$('#proj-unadopted').addEventListener"):]
    assert "'/api/projects/adopt'" in adopt, "Adopt is not wired to the adopt endpoint"
    assert "refused: ${d.detail}" in adopt, "a refused Adopt is silent"


def test_the_projects_page_reads_every_number_it_shows():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects.html")

    assert "'/api/projects'" in script, "the page never reads the projects payload"
    assert "renderSummary(d.summary)" in script and "renderRows(d.projects)" in script \
        and "renderUnadopted(d.scan)" in script, "a section of the payload is never rendered"
    assert "request failed" in script, \
        "a failed request leaves the page showing last week's registry with no note"
    assert "innerHTML" not in script, "the page renders registry data as markup"


def test_the_projects_page_is_linked_and_routed():
    base = (ROOT / "web" / "templates" / "base.html").read_text()
    assert 'href="/projects"' in base, "the Projects page is not in the nav"
    main = (ROOT / "app" / "main.py").read_text()
    assert '@app.get("/projects")' in main, "the nav links a page that has no route"
    assert "projects.html" in main, "the route renders a template that is not the projects page"


def test_the_projects_page_promises_the_name_rule_the_server_enforces():
    """The page states the rule and the server enforces it. They are checked against the same
    sentence, so the page cannot promise a name the endpoint refuses."""
    from app.core import projects as proj

    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    needle = "lowercase letters, digits and single hyphens"
    with pytest.raises(proj.ProjectError) as exc:
        proj.validate_name("My-App")
    server_rule = str(exc.value)

    assert needle in tpl.lower(), "the page does not state the name rule the operator will hit"
    assert needle in server_rule.lower(), "the server's rule sentence changed shape: the page's copy is stale"
    for shape in ("my-app", "My-App", "app--1", "app/x", "a"):
        ok = bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", shape)) and len(shape) >= 2
        refuses = False
        try:
            proj.validate_name(shape)
        except proj.ProjectError:
            refuses = True
        assert refuses is (not ok), f"{shape}: the page tells the operator {ok}, the server answers {not refuses}"


def test_the_projects_page_offers_opening_text_and_a_remote():
    """§6.14.1n, the operator's additions: README.md / AGENTS.md text fields, a remote, and the
    pull-first option. The fields exist, and the create call carries all of them."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")

    for el in ("proj-readme", "proj-agents", "proj-remote", "proj-pull"):
        assert f'id="{el}"' in tpl, f"the create card is missing {el}"
    assert "<textarea id=\"proj-readme\"" in tpl and "<textarea id=\"proj-agents\"" in tpl, \
        "the opening text is a single-line input: README.md and AGENTS.md are documents"

    post = script[script.index("req('POST', '/api/projects'"):]
    for field, el in (("readme_text", "proj-readme"), ("agents_text", "proj-agents"),
                      ("remote", "proj-remote"), ("pull_first", "proj-pull")):
        assert field in post, f"the create call does not send {field}"
        assert f"$('#{el}')" in post, f"the create call does not read {el}"
    assert "d.remote" in post, "the page creates a project and never reports what the remote became"


def test_the_page_states_what_pull_first_does():
    """The pull order is the whole design of the remote field: the repository's files arrive before
    the templates. The page says so, in the same words the tests prove."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    assert "before the init files are written" in tpl
    assert "origin" in tpl, "the page does not say the remote becomes origin"


# ------------------------------------------------------------------ the harness seed
#
# §6.14.1o: the host is seeded with the agent config it reads, before it starts. The
# page has to say which of the two the running host is, because the difference is
# invisible to the operator otherwise: a seeded host writes with the agent's posture,
# an unseeded one writes with defaults, and both look identical from the browser.

def test_the_projects_page_reports_the_seed_the_harness_host_carries():
    """§6.14.1o: the page reads the host's seed off the status endpoint and says seeded,
    unseeded, and not started — three states, the last one not spelled 'unseeded'."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")

    assert "/api/harness/status" in script, "the page never reads the harness host's state"
    # The seed is read off the payload. A page that spelled it from its own config file
    # would report a seed the running host does not carry.
    assert "seed.files" in script, "the page reads the status and drops the seed"
    assert "seed.seeded" in script, "the page never asks the host's payload whether it is seeded"
    assert "seeded" in script and "unseeded" in script

    # The third state: a host that is not running has no seed to carry, and the page
    # must not call that "unseeded" (it never read anything) nor "seeded".
    seed_row = script[script.index("harnessSeed"):] if "harnessSeed" in script else script
    assert "not started" in seed_row, "a host that is not running is reported by its seed state"


def test_the_projects_page_renders_the_seed_at_page_load_and_on_refresh():
    """A seed that is only read when the operator clicks something is a seed that is
    stale at load — the host may have been started from the command line."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    refresh = script[script.index("async function refresh"):]
    refresh = refresh[:refresh.index("$('#proj-plan')")]
    assert "harnessSeed" in refresh, "refresh() does not refresh the seed"


# ------------------------------------------------- the derived .gitignore (queue 18, the operator's rule)
#
# The operator's words: checking "git repository" should produce .gitignore, filled from
# the stacks that were picked. That makes the file a consequence, not a box — so the page
# has to say what it wrote and from what, because a file the operator never ticked and
# never sees named is a file they will find in `git status` and not recognize.

def test_the_projects_page_says_the_gitignore_arrives_with_the_boxes_not_as_one():
    """The shortlist must not offer .gitignore as a box; the page says where it comes from.

    The boxes come from `INIT_STEPS`, so the page has no hard-coded list to check here — what is
    checked is the sentence the operator reads beside the boxes, because that is where a
    consequence has to be announced.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    assert 'value="gitignore"' not in tpl, "the page offers a .gitignore box of its own"
    # The paragraph the operator reads before ticking anything — the one the boxes live under.
    intro = tpl[tpl.index('id="proj-namerule"'):]
    intro = intro[:intro.index('id="proj-steps"')]
    assert ".gitignore" in intro, "the boxes are shown with no word about the file they imply"
    assert "not a box" in intro, "the page does not say the file is a consequence, not a choice"


def test_the_projects_page_reports_the_ignores_it_wrote_after_a_create():
    """The plan and the create result both name the file: the plan so the operator sees it
    before creating, the result so the operator sees what actually happened."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    plan = _fn(script, "plan")
    assert "gitignore" in plan, "the plan does not mention the file it will write"
    create = script[script.index("#proj-create')"):]
    assert ".gitignore" in create, "the create result drops the file the page says it wrote"


# --------------------------------------------- the project is being served (§6.14.1n, queue 33's connection)

def test_the_projects_endpoint_answers_which_projects_are_being_served(client):
    """The page's Launch handoff is only honest if the page can say what is already launched. The
    payload carries the host's answer, and the page's rows read it (D1)."""
    body = client.get("/api/projects").json()
    assert "served" in body, "the payload cannot say what is launched"
    assert {"count", "names", "hostOk", "unknown"} <= set(body["served"]), body["served"]
    for row in body["projects"]:
        assert "serve" in row, f"the row for {row['name']} cannot say whether it is served"


def test_the_projects_page_shows_the_serving_state_of_every_row():
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    assert "serve" in script, "the page never reads the serving state the payload carries"
    assert "is being served" in tpl or "being served" in tpl, \
        "the page has no words for a project that is running"


# ------------------------------------ the projects page launches a project (queue 33's connection)

def test_the_projects_page_launches_through_the_verb_that_records_the_pair():
    """The page's Launch is the operator's launch of a project. It posts to the endpoint that
    creates the session inside the project folder **and records the session and the personality**
    (§6.14.1n), and it refreshes so the row's serving state comes from the payload, not from the
    page's memory of what it clicked (D1).

    Asserted on the line that handles the click, the way this file asserts the other row verbs:
    a string that also appears in the page's CSS proves nothing about the control it renders.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")

    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]
    launch = rows[rows.index("dataset.launch"):]
    launch = launch[:launch.index("dataset.forget")]

    assert "/api/projects/launch" in launch, "Launch is not wired to the endpoint that records the pair"
    assert "req(" in launch, "Launch is a button that does not act"
    assert "refresh(" in launch, "Launch does not re-read the payload; the row keeps the browser's memory"


def test_the_projects_page_names_the_project_launch_it_refuses():
    """A launch can be refused — the host is not running, the folder is not in the registry, the
    personality is unknown. The summary has to carry the refusal, as it does for every other row
    verb: an error-swallowing button is indistinguishable from one that worked."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]
    launch = rows[rows.index("dataset.launch"):]
    launch = launch[:launch.index("dataset.forget")]
    assert "detail" in launch, "the launch's refusal is dropped"


def test_the_projects_payload_states_the_launch_verb_the_page_uses(client):
    """The page's Launch and the endpoint behind it are one fact, stated by the portal.

    The payload names the endpoint the row posts to and says whether the host can take a launch at
    all. A page that invented both would keep the row inert while the endpoint existed, or post to a
    verb that does not exist while the host was down — and a *contract* test that asserted the
    launch's answer shape would pass on a payload that described a verb no row used.
    """
    body = client.get("/api/projects").json()
    assert "launch" in body, "the payload cannot say how a project is launched"
    assert {"endpoint", "available"} <= set(body["launch"]), body["launch"]
    assert body["launch"]["endpoint"] == "/api/projects/launch", body["launch"]
    assert isinstance(body["launch"]["available"], bool), body["launch"]


# ------------------------------- the personality half of the link is recorded by the launch (queue 33)

def test_the_projects_page_lets_the_operator_choose_the_personality_the_session_runs_with():
    """Queue 33: "when a project is launched, a personality is picked and the session is created,
    recording the link." A picked personality is a choice the operator makes, so the page has to
    present the personalities the portal knows — read off the payload, not spelled from a list the
    page maintains (D1)."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")

    assert 'id="proj-personality"' in tpl, "the page launches without a personality choice"
    assert "<select" in tpl and "proj-personality" in tpl, "the choice is not a control"

    # The options come from the payload. A page that hard-coded two names would launch a session with
    # a personality the operator renamed on the personalities page, and the row would record a name
    # that no longer exists.
    render = script[script.index("function renderPersonalities"):]
    render = render[:render.index("function renderRows")]
    assert "PERSONALITIES" in render, "the options are not stored from the payload"
    refresh = script[script.index("async function refresh"):]
    refresh = refresh[:refresh.index("$('#proj-plan')")]
    assert "renderPersonalities" in refresh, "the personality list is not refreshed with the payload"
    assert "summary" in refresh, "the personality list is not read from the payload's facts"


def test_the_project_row_shows_the_personality_the_launch_recorded():
    """The row is the project's half of the link: "project → the personality it is served with."
    The name has to come from the payload's serving fact, because it is the launch's record that put
    it there (§6.14.1n / queue 33)."""
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    rows = script[script.index("function renderRows"):]
    rows = rows[:rows.index("function renderUnadopted")]
    assert "st.personality" in rows, "the row shows the serving state without the personality"


def test_the_payload_carries_the_personalities_the_page_offers(client):
    """The personality names the launch records must be the portal's names, in the payload the page
    reads. Asserted on the payload rather than the page: a page can spell a select, and only the
    payload says which personalities actually exist on this box.

    The two built-ins the portal guarantees are in it, and it is a list of names — a dict of
    personalities would leave the page assembling labels the row then records as a personality.
    """
    body = client.get("/api/projects").json()
    assert "personalities" in body["summary"], "the payload has no personality list for the page"
    names = body["summary"]["personalities"]
    assert isinstance(names, list) and all(isinstance(n, str) for n in names), names
    assert names, "the payload offers an empty personality list"


def test_the_projects_page_says_what_the_launch_recorded():
    """The operator's sentence after a launch carries the record, not just the click: the project,
    the personality it was launched with, the session, and whether the pair reached the project's
    row.

    `d.recorded` is the fact the projects page's serving state is built from, so a page that
    dropped it would report "the row below shows it as served" for a launch the registry never
    heard about — the exact gap queue 33 is about.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]
    launch = rows[rows.index("dataset.launch"):]
    launch = launch[:launch.index("dataset.forget")]

    for fact in ("d.recorded", "d.session", "d.personality", "d.reason"):
        assert fact in launch, f"the launch's answer drops {fact}"
    assert "recorded" in launch and "not recorded" in launch, \
        "a launch the registry never heard about is reported as a record"


def test_the_launch_says_what_it_will_start_before_it_starts():
    """A launch opens a session on a host that outlives the portal, so the confirm names both sides
    of the pair it is about to record — the project and the personality.

    The row's Launch is not a destructive verb (it deletes nothing, and the endpoint's own
    `confirm` is what the sweep is exempt from), so this is the page's own confirmation, not the
    sweep's: an operator who clicked the wrong row's button needs to see which project and which
    personality they are about to start, in the dialog.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    rows = script[script.index("$('#proj-rows').addEventListener"):]
    rows = rows[:rows.index("$('#proj-unadopted').addEventListener")]
    launch = rows[rows.index("dataset.launch"):]
    launch = launch[:launch.index("dataset.forget")]

    assert "window.confirm(" in launch, "a launch starts a host session without asking"
    dialog = launch[launch.index("window.confirm("):launch.index("window.confirm(") + 600]
    assert "name" in dialog and "personality" in dialog, \
        "the launch confirm does not name both halves of the pair it records"
    assert "confirm: true" in launch, "Launch posts without the confirm the endpoint requires"


def test_the_launch_is_disabled_by_the_shadowed_host_the_page_reports():
    """Queue 30's shadow, queue 33's record. When the unit is not the listener, every session on the
    port belongs to a process the portal does not supervise, so the pair the launch records is a fact
    about a session the portal cannot list — the project's row would read as served with a session
    nobody owns.

    The page disables Launch from the payload's `available`, and the payload's `available` is false on
    a shadowed host with the harness page's own sentence as the reason (see
    tests/test_projects.py::test_a_shadowed_host_is_not_a_host_a_launch_can_be_recorded_on). What this
    test pins is the page's half: the *reason* is shown in the control's own words, so a disabled
    button is not a mystery.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    rows = script[script.index("function renderRows"):]
    rows = rows[:rows.index("function renderUnadopted")]
    build = rows[rows.index("const launch = el("):]
    build = build[:build.index("const forget = el(")]

    assert "launch.disabled" in build and "available" in build, \
        "Launch is not disabled by the payload's launch state"
    assert "reason" in build, "a disabled Launch does not say why the host cannot take it"


def test_the_launch_reason_is_shown_as_text_the_operator_can_read():
    """A disabled button that explains itself only in a tooltip is a disabled button with a secret.

    Queue 30's shadow and queue 33's host-down state both arrive as `launch.reason` on the payload
    (§6.14.1q). Putting it in `title` alone means the operator must hover the exact control to learn
    why their verb is unavailable — on a touch device, never. The reason is painted as visible text on
    the host line, from the payload's own words.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    host = _fn(script, "harnessSeed")

    assert "title" not in host, "the host line's explanation lives in a tooltip"
    assert "reason" in host, "the host line does not carry the payload's reason"


def test_the_projects_page_says_when_a_launch_records_a_name_the_store_has_not():
    """§6.14.1n: a box with no stored personalities launches with the registry's `default`, which is
    synthesized from defaults and is not a personality on the personalities page.

    The payload states that fact (`summary.personalities_named`, tests/test_projects.py). The page
    must say it, because the alternative is the projects page reporting "served with default" next to
    a personalities page with no personality named `default` — the two pages disagreeing about a fact
    visible on both.
    """
    tpl = (ROOT / "web" / "templates" / "projects.html").read_text()
    script = _script_of(tpl, "projects")
    summary = _fn(script, "renderSummary")
    render = _fn(script, "renderPersonalities")
    said = summary + render

    assert "personalities_named" in said, "the page ignores the payload's statement about its list"


def test_the_personality_editor_shows_the_projects_that_run_with_it(client):
    """Queue 33's page half (§6.14.1q): the editor's `used by` line, wired to the payload.

    The brief states the connection in both directions. The record half is on the projects page;
    this is the personality side, on the surface where a personality is changed — a personality
    edited without knowing a project runs with it is edited blind, because the change lands on that
    project's next launch.

    Three properties, each a way the line goes quietly wrong:

    - the answer is *read from the payload*, not inferred from the page's own model list. The page
      knows personalities and models; it does not know sessions, and a page that assembled "used by"
      from its own data would be inventing the host's fact.
    - the line is per personality and updates when the editor moves to a different one; a sentence
      computed once at load describes whichever personality happened to be selected then.
    - when the host did not answer, the page carries the payload's reason. An empty line there is
      the page claiming it checked.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert 'id="ed-used-by"' in tpl, "the editor has no `used by` line"

    script = _script_of(tpl, "personalities")
    assert "used_by" in script, "the line is not read from the payload's answer"

    # The line moves with every other step-1 fact: it is painted by the same handler that syncs
    # the identity row, so switching personality repaints it.
    sync = _fn(script, "syncIdentity")
    assert "UsedBy" in sync, "the line is not refreshed when the editor moves"

    # The host's reason reaches the operator, in text.
    said = "".join(_fn(script, f) for f in ("usedByLine",) if f"function {f}(" in script)
    assert "reason" in said, "the line ignores the payload's reason for an unanswered host"

    page = client.get("/personalities").text
    assert 'id="ed-used-by"' in page, "the line is not rendered"


# --------------------------------------------------- §6.14.1q state 3: the editor as steps
#
# The page's one measured defect (docs/PORTAL_LAYOUT.md, L6): one block taller than three viewports.
# tools/layout_measure.py on the deployed code: /personalities is 6,430 px in three blocks, and the
# tallest is `2 Global ([*])` at 5,335 px on a laptop — 10,295 px on a phone. The deck is 484 px. The
# height is the editor, printed as one long column of every field of every key.
#
# The approved shape is the editor **opened by a verb, as steps**. These assertions are the shape,
# each one a way the "fix" degenerates:
#   - a `display:none` toggle with no state: a control that hides content and says nothing about
#     what is visible is a layout trick, not a state. The state lives on the element, as an attribute
#     the page's own deck already uses (`data-raised`), and every step is reachable.
#   - a collapsed control with no words: a `>` that closes the editor leaves no way back.
#   - steps with no names: a page that says "2" and "3" is an index, not a step an operator can aim at.
#   - a second copy of the fields under a tab: two grids means two sources of truth for one setting.
#
# The fields themselves are asserted by the existing editor tests (row data, the one-object-at-a-time
# rule, the shared-config cards). Nothing here moves a field; it changes what is visible at once.


def _surface_section(script: str) -> str:
    """The card-surface state code, bounded by its own two markers.

    The surface layer is deliberately NOT inside loadPersona/syncIdentity/summarize: the editor
    tests read those functions by name, and a card handler absorbed into an editor function would
    be claiming that opening a card is part of loading a personality (that absorbed-handler
    mistake is recorded in plan §6.14.1q's findings). A named marker is how a layer says where it
    lives — and the section is bounded, because a layer that runs to the end of the script is not
    a layer: it absorbs every handler written after it, and a rule about that layer would be a
    rule about the page.
    """
    start = script.find("// SURFACE:")
    assert start >= 0, (
        "the surface layer is not marked. The editor's functions are read by name by this file's "
        "other tests, so the card-state code has to declare where it lives"
    )
    end = script.find("// SURFACE END:", start)
    assert end >= 0, "the surface layer does not declare where it ends"
    return script[start:end]


def test_the_editor_is_a_card_surface_not_a_stepper(client):
    """The owner's Oct-18 instruction, as structure: one surface of cards, no stepper.

    The stepper measured as a hot mess: tab-shaped links replacing the whole editor with no
    back/forward. A surface answers the same height problem honestly -- collapsed cards, one
    field grid per scope -- while every region (identity, cards, review) stays on the page at
    once, so "which step am I on" is not a question the page can ask. The Global card is pinned
    (edited, never removed); model cards join and leave the surface with the tick list. State
    is attributes on the elements (`data-ed-view`, `data-open`, `data-on-surface`) -- the page's
    own deck idiom -- not classes the script flips.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    # The stepper is gone: no step state, no step sections, no stepper list.
    assert "data-ed-step" not in tpl, "the editor still carries step state"
    assert 'id="ed-steps-list"' not in tpl, "the stepper list is still on the page"
    assert "data-ed-step-name" not in tpl, "the sections are still step-swapped"
    # The three regions, present together, and one wrapper for the collapse verb.
    for sec in ('id="ed-identity"', 'id="ed-surface"', 'id="ed-review"'):
        assert sec in tpl, f"the surface is missing {sec}"
    assert 'id="ed-workbench"' in tpl, "the collapse verb has no single wrapper to hide"
    # The frame names its personality in two places that repaint with the select (owner
    # Oct-18: nothing said the cards belonged to the personality being edited).
    assert 'class="ed-frame"' in tpl, "the editor has no frame"
    for who in ('id="ed-frame-who"', 'id="ed-surface-who"'):
        assert who in tpl, f"the frame does not name its personality ({who} missing)"
    # The Global card: pinned, saying so, and hosting the one [*] grid. No control on its strip:
    # the strip reads and opens; the pin sentence is text, not a locked button.
    assert 'id="ed-card-global"' in tpl and 'data-pinned="yes"' in tpl, "the Global card is not pinned"
    card = tpl[tpl.index('id="ed-card-global"'):tpl.index('id="mp-cards"')]
    assert 'id="ed-global"' in card, "the Global card does not host the [*] grid"
    assert "always on the surface" in card, "the pinned card does not say it is pinned"
    strip = card[card.index('class="mp-strip"'):card.index("</button>")]
    assert "<button" not in strip and "<input" not in strip, "the Global strip carries a write control"
    # Model cards carry surface membership and the CSS hides off-surface cards.
    assert "data-on-surface" in tpl, "model cards do not carry surface membership"
    assert '.mp-card[data-on-surface="no"]{display:none}' in tpl, "off-surface cards are not hidden"
    assert 'class="mp-remove"' in tpl, "the card has no remove-from-surface verb"

    script = _script_of(tpl, "personalities")
    surf = _surface_section(script)
    assert "data-ed-view" in surf, "the collapse state is not what the script writes"
    assert "dataset.open" in surf, "the card open state is not what the script writes"
    assert "onSurface" in surf, "surface membership is not painted from the tick list"
    assert re.search(r"textContent", surf), "the state is not stated in text an operator reads"


def test_the_models_library_is_a_tree_not_a_wall(client):
    """Owner Oct-18: the 12-column table was "barely useable" -- the model id overflowed
    its column on virtually every row and obscured the columns after it. The library is a
    tree now: quantizer -> model -> ONE BRANCH PER QUANT (the id's own metadata:
    quantized_by/name/file_type -- the index's unique key, so every branch is exactly one
    model), a filter beside it, and the model's card is the branch's body (owner: "a model
    card never has to share space with another model"). The card is a dense, organised
    breakdown -- every fact the index stores, grouped into what the model IS, what it
    costs in RAM and disk, and what it can DO -- so an operator can pick a model for a
    task, decide which models fit resident together, and see what eats the disk.
    The wall's markers (#library-table, sticky-column CSS) must stay gone.
    """
    tpl = (ROOT / "web" / "templates" / "models.html").read_text()
    assert 'id="mv-tree"' in tpl and 'id="mv-filter"' in tpl, "no tree, no filter"
    assert '<details class="mv-quant" data-model=' in tpl, \
        "quants are not branches with the card as their body"
    assert 'id="library-table"' not in tpl and ".grid-wrap" not in tpl, "the wall is back"
    # Card sections: the owner's questions each need fields to answer.
    for sec in ("on the router", "identity", "size and disk",
                "memory (portal estimate", "architecture", "inputs and features"):
        assert f'<div class="mv-sec">{sec}' in tpl, f"card section missing: {sec}"
    for label in ("id on router", "model id", "gguf name", "quantizer (repo)",
                  "quantization", "header file_type", "architecture", "source", "size (all shards)", "shards",
                  "sidecar file", "path", "weights", "KV cache", "total", "peak RAM",
                  "ctx (trained)", "layers", "full-attention interval", "attention heads",
                  "kv heads", "embedding length", "rope dim", "vocab size", "MoE",
                  "ssm state", "chat template", "vision", "fill-in-middle",
                  "self-draft layers", "draft head", "pooling", "capabilities",
                  "fits here", "max ctx estimate", "priced at ctx"):
        assert f'<span class="k">{label}</span>' in tpl, f"card field missing: {label}"
    assert "{{ g.bytes | sz }}" in tpl and "{{ s.bytes | sz }}" in tpl, \
        "group summaries must state their total on disk (the disk-space question)"
    script = tpl[tpl.index("getElementById('mv-tree')"):]
    assert "addEventListener('toggle'" in script and "o.open = false" in script, \
        "cards are dense: opening a branch must close the others"
    assert "mv-filter" in script and "dataset.search" in script, "filter is not wired"
    assert "n.tagName === 'DETAILS'" in script, "the open branch's path is not opened"
    assert "live.length === 1" in script and "for (const l of live) openPath(l)" not in script, \
        "boot must not pre-open the tree: llama.cpp registers every model it knows"
    assert "'.mv-live'" in script, "live detection needs the dedicated marker (caps share .tag.ok)"


def test_form_controls_read_as_controls(client):
    """Owner Oct-18 (the whole portal): fields were --panel on --panel with 1px --line edges --
    "the background looks identical to the rest of the panel ... borders hardly noticeable,"
    and a checkbox disabled by design (a built-in's list) looked exactly like a live one, so
    the refused click was silent. The contract is the tokens and the rules that answer it:
    a recessed --field fill, a 1.5px --field-line edge, a disabled rule that recedes, the
    not-allowed cursor at the label wrapping the disabled input, a pointer cue on clickable
    disclosures, and disabled buttons that read disabled.
    """
    base = (ROOT / "web" / "templates" / "base.html").read_text()
    assert "--field:#0b0e13" in base and "--field-line:#3d4a5c" in base, "no field vocabulary"
    assert "background:var(--field)" in base, "fields do not recede from the panel"
    assert "1.5px solid var(--field-line)" in base, "control edges are the panel hairline again"
    assert "input:disabled,select:disabled,textarea:disabled" in base, "disabled fields are not stated"
    assert "label:has(>input:disabled){cursor:not-allowed" in base, "the refused click has no cursor"
    assert "details>summary{cursor:pointer}" in base, "clickable sections offer no pointer cue"
    assert "button:disabled" in base, "disabled buttons must read disabled too"


def test_the_editor_only_offers_what_can_be_edited(client):
    """Owner Oct-18: "if a personality cannot be edited like safe and default they should not
    appear as options in the editor to be edited." The deck is the reading surface -- built-in
    cards say "built-in, not editable in place" there; the editor's select carries only the
    editable personalities and the new mode. A select that offers a locked record is a select
    whose first act is a refusal.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert "{% for c in cards if c.editable %}" in tpl, "the select offers built-ins to edit"
    # The identity row: the name shows for every personality (read-only for stored records --
    # the name IS the record), and the count field labels itself in words (owner Oct-18:
    # the bare "max resident" read as part of "name").
    assert 'id="ed-new-name" type="text" autocomplete="off" spellcheck="false"\n' in tpl, \
        "the name field hides from existing personalities"
    assert ".readOnly = !isNew" in tpl, "the stored name is editable text, not a stated record name"
    assert "max resident models" in tpl, "max resident is still labeled like a name"


def test_the_model_tree_is_cards_that_show_their_state(client):
    """Owner Oct-18: "embrace the card metaphor ... rather than showing a checkbox give each
    its own card border and change the border, background, and text color to indicate that
    model is selected." The checkbox stays in the DOM -- it is the store the personality
    saves and the control the harness clicks -- drawn invisibly inside the card. Selected =
    accent edge/tint/name via :has(:checked); disabled (built-in) = dashed and muted AT THE
    CARD, with the refusal sentence beside the cards (`ed-attach-cards-note`); and a card
    arriving on the surface flashes and scrolls into view, because the tick is clicked at
    the bottom of a long page and the card arrives at the top, out of view."""
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    for needle in (".ed-attach-item:has(input:checked)", ".ed-attach-item:has(input:disabled)",
                   ".ed-attach-item:focus-within", ".mp-card.ed-flash",
                   'id="ed-attach-cards-note"'):
        assert needle in tpl, f"tree card contract missing: {needle}"
    # Alias rows stack inside one card: alias carries the row, `↳ base` sits below it
    # (owner Oct-18: two long names side by side wrapped to mush).
    for needle in (".ed-alias-item{flex-direction:column", "ed-alias-main", "↳ {{ o.base }}"):
        assert needle in tpl, f"alias row contract missing: {needle}"
    assert "position:absolute;opacity:0" in tpl, "the checkbox is not the card itself"
    script = _script_of(tpl, "personalities")
    assert "ed-attach-cards-note" in script, "the click-site sentence is not painted"
    assert "checked.includes(mid)" in script, "the surface reads the saved list, so a fresh tick is invisible"
    assert "ed-flash" in script and "scrollIntoView" in script, "an arriving card gives no signal"
    assert "paintSurface.quiet" in script, "boot must not flash or scroll (every card arrives at once)"


def test_the_editor_collapses_behind_one_labeled_toggle(client):
    """Owner Oct-18: not a "close the editor" button — the frame carries ONE collapse toggle.

    One button, never hidden, whose label and aria-expanded state which way it acts. The
    open/close verb pair asks the operator to find a control that exists in only one state —
    and this page's own history (the Now strip, §6.14.1q) records what a control carried by
    one state only does to a script: it is found in half the states the page can be in. The
    frame is the collapsible thing; `#ed-workbench` is the one wrapper the toggle hides, and
    the Oct-15 rule stands: the editor opens open.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert 'id="ed-toggle"' in tpl, "the frame has no collapse toggle"
    assert 'id="ed-open"' not in tpl and 'id="ed-collapse"' not in tpl, "the verb pair is back"
    assert 'id="ed-workbench"' in tpl, "collapse has exactly one wrapper to hide"
    css = tpl[tpl.index('.pers-editor[data-ed-view="collapsed"]'):]
    assert "#ed-workbench{display:none}" in css[:160], "collapse does not hide exactly the workbench"
    script = _script_of(tpl, "personalities")
    surf = _surface_section(script)
    assert "ed-toggle" in surf, "the toggle is not wired by the frame layer"
    assert "aria-expanded" in surf, "the toggle does not state its state"
    assert re.search(r"textContent", surf), "the toggle's label states the state in words"
    assert "collapsed" in tpl, "the collapsed state is not named on the page"


def test_the_card_surface_opens_cards_without_moving_fields(client):
    """The card layer shows and hides; it does not rebuild fields, copy grids, or write values.

    The grid ids stay singletons — a card layer implemented by duplicating the field grids is
    two sources of truth for one setting, which is the failure the editor's own tests were
    written for. The layer's own markers bound the assertion: a layer that runs to the end of
    the script absorbs every handler written after it.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert tpl.count('id="ed-global"') == 1
    assert tpl.count('id="ed-model-fields"') == 1
    assert tpl.count('id="ed-model"') == 1, "the target <select> id must stay the single one"

    script = _script_of(tpl, "personalities")
    surf = _surface_section(script)
    # The layer moves card state, and nothing else: no field internals, no requests, no writes
    # into a control. (showCard moving the one grid element is the card layer's own honest verb,
    # asserted where it lives: the model-personality card test.)
    for forbidden in ("innerHTML", "querySelectorAll", "ed-field", ".value =", "fetch(", "postConfig"):
        assert forbidden not in surf, f"the card layer touches field internals ({forbidden})"


def test_the_deck_strip_carries_the_budget_figure_it_computes(client):
    """Queue 33's 30-second glance: the strip line answers *can this run* without a click.

    The deck's strip measured 43 px per card (49 px with the gap) — the deck is already dense
    enough, so the glance is not a height problem. What a strip cannot answer today is the
    question the badge asserts: a card reads "worst case fits" with no figure beside it, so the
    operator has to raise the card to see what "fits" was measured against.

    The figure is the payload's own (`generated[i]['budget']`, the same `Budget.as_dict()` the
    card body prints). This asserts the strip's line carries cost + aperture, so the badge and
    the number cannot disagree by being printed in different places.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    strip = tpl[tpl.index('class="pers-strip"'):tpl.index('<div class="pers-body">')]
    assert "badge" in strip, "the strip carries no badge at all"
    assert "fits" in strip, "the strip's fits verdict is not in the strip"
    assert "worst_case_bytes" in strip and "budget_bytes" in strip, (
        "the strip states a verdict with no figure: 'fits' is unanchored until the number "
        "the verdict came from is on the same line")
    # The figure must be read from THIS card's generated row. `loop.index` is 1-based and
    # `generated` is ordered by card, so a strip built with `loop.index` prints the NEXT card's
    # budget — a number that is real, formatted, and belongs to somebody else.
    assert "generated[loop.index0]" in tpl[tpl.index('<div class="pers" id="pers-deck">'):tpl.index('class="pers-strip"')], (
        "the deck's per-card budget row is not indexed 0-based")
    # The strip line must stay one line: the deck's density claim is 49 px per personality,
    # and a strip that wraps onto a second line is 71 px and a 710 px deck for ten rows.
    css = tpl[tpl.index(".pers-strip{"):tpl.index(".pers-strip:hover")]
    assert "white-space:nowrap" in css, "the strip line is allowed to wrap"
    assert "overflow:hidden" in css and "text-overflow:ellipsis" in css, (
        "a strip that overflows must clip, not push the page wider")
