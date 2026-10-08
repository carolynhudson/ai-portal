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
    assert 'id="machine-detail"' in html, (
        "the dashboard has no machine detail section, so the merge left disk, GPU "
        "and the chart with nowhere to live")


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


def test_the_static_metric_cards_say_when_they_were_sampled(pages):
    """Disk and GPU in the dashboard's machine detail are rendered once, at page load.

    A page headed "Live metrics" invites the reader to treat every number on it as
    current. The polled cells prove their own freshness by moving; the two cards that
    never move have no such cue, so a 20-minute-old disk figure and a wrong one are the
    same sight. The fix is the sample time, printed with them.
    """
    html = pages["/"]
    stamps = re.findall(r"sampled\s*(\d{1,2}:\d{2}:\d{2})", html)
    assert len(stamps) >= 2, (
        "the Disk and GPU cards carry no sample time — a rendered-once number is "
        "indistinguishable from a live one")
    # The polled line labels itself as polled, so the two kinds of number read apart.
    assert 'id="machine-polled"' in html, "the chart line does not report its poll time"

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
    assert "d.open && !document.hidden" in tpl, (
        "the machine-detail poll runs while the panel is closed — it is the only "
        "reason the panel can say 'polled 12:03:11'")

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
                "delete-build-btn", "purge-stray-btn"):
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
    pairs = {"#comfy-upd-btn": "loadComfyUpd", "#hf-upd-btn": "loadHfUpd"}
    for sel, loader in pairs.items():
        start = tpl.index("function " + loader + "(")
        end = next(i for i in range(start + 1, len(tpl)) if tpl[i:i + 9] == "function ")
        body = tpl[start:end]
        btn = sel.lstrip("#")
        assert btn in body, (
            f"{sel} is enabled outside {loader}(), so its lock is not tied to its panel")
        assert re.search(r"getElementById\('" + btn + r"'\)", body), (
            f"{loader}() fills the panel without touching {btn}")


def test_the_reference_links_come_from_the_row_and_not_from_the_template():
    """The anchors are row data, so the template names the field, never a key.

    The point of §6.14.1 is that a registry key is a data edit. That survives only while the
    template's link is `{{ r.settings_ref }}`: a template carrying `href="/help#ctx-size"` is a
    page that knows the registry's contents, and a key added tomorrow gets a row with no link.
    """
    tpl = (ROOT / "web" / "templates" / "personalities.html").read_text()
    assert tpl.count('href="/help#{{ r.settings_ref }}"') == 3, \
        "the reference link belongs in the keys table and in both editor grids"
    assert not re.search(r'href="/help#[a-z][a-z0-9-]*"', tpl), \
        "the template hardcodes a settings anchor; the anchor is row data"
    assert 'href="/help#{{ r.help_ref }}"' in tpl, "the plan-section link is gone with it"


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
    assert "if (f.hidden) continue;" in ed, "capture reads hidden rows"
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
    assert "background:var(--panel);color:var(--fg);border:1px solid var(--line)" in base, \
        "controls are painted with values from outside the page's own palette"
    assert "select>option{background:var(--panel);color:var(--fg)}" in base, \
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
