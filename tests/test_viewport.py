"""The phone probe as a standing test. (Owner + partner, 2026-11-02.)

The site is desktop-tuned; the partner's read is "dense, built for desktop",
measured here in px. This module is the measurement instrument the owner
asked for **before** design changes: it renders every spec page in Chromium
at three phone viewports and turns breakage into numbers.

The pinning discipline follows the background-tone precedent (6.14.1w): what
passes today is pinned as an invariant; what fails today is written to the
report as an enumerated finding and gets fixed in design rounds, each of
which pins its own fix. A test that fails on day one is noise; a test that
pins the floor and names the ceiling is a map.

Pinned today (they pass):
  * every page declares `width=device-width` (without it a phone renders a
    980px desktop canvas - the site is genuinely viewport-aware);
  * landscape 844px overflow has not grown past its measured floor (the nav
    logout form caused the original 42px; the icon-nav round lowered the
    floor to 0, and growing it is a regression);
  * the probe covers every authed page of the UI census;
  * the nav is phone-ready on EVERY page (nav icons round, 6.14.1ze): the
    nav row overflows 0 at 390px portrait and every nav target (7 links +
    logout) measures >= 44px -- the acceptance gates the owner pinned.

Enumerated, not pinned (the 2026-11-02 findings, from ~/ai/logs/viewport-20261102):
  * [RETIRED 2026-11-03, 6.14.1ze: portrait ~496px overflow - the nav's
    886px flex row; the icon nav fits at 0. The page-level portrait
    overflow finding stays open for the remaining offenders below.];
  * [RETIRED 2026-11-03, 6.14.1ze: 23px nav links; pinned >= 44px above];
  * form fields are 14-15px - iPhone Safari zooms the page on focus;
  * /settings svc-ports and agents-table need sideways scroll with no shell;
  * /models has 19 spans at 11px.
"""
import json
import pathlib

import pytest

from tests.test_ui_integration import SPEC, ui_server  # noqa: F401

PORT_PORTRAIT = 390
LANDSCAPE = 844
# the measured 2026-11-02 landscape overflow (nav logout form) was 42px;
# the nav icons round (6.14.1ze) brought it to 0, which is the new floor.
# A design round may lower it; growing it is a regression and fails.
LANDSCAPE_OVERFLOW_FLOOR = 0


def _probe(ui_server):  # noqa: C901 - the probe's plumbing, not branch logic
    from tools.viewport_probe import VIEWPORTS, MEASURE
    from playwright.sync_api import sync_playwright

    pages = [p["path"] for p in SPEC["pages"] if p.get("auth", True)]
    out: dict = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        name, value = ui_server["cookie"].split("=", 1)
        ctx.add_cookies([{"name": name, "value": value, "url": ui_server["base"]}])
        page = ctx.new_page()
        for path in pages:
            page.goto(f"{ui_server['base']}{path}", wait_until="networkidle")
            page.wait_for_timeout(500)
            per = {}
            for vname, (w, h) in VIEWPORTS.items():
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(250)
                per[vname] = page.evaluate(MEASURE, w)
            out[path] = per
        b.close()
    return out


@pytest.fixture(scope="module")
def probe(ui_server):
    return _probe(ui_server)


def test_every_page_declares_the_viewport_meta(probe):
    """Without the meta tag a phone invents a 980px canvas; every measured
    phone failure below presumes the site chose device-width deliberately."""
    for path, per in probe.items():
        for vname, d in per.items():
            assert d["hasViewportMeta"], f"{path} at {vname} has no viewport meta"


def test_landscape_overflow_does_not_grow_past_the_measured_floor(probe):
    for path, per in probe.items():
        got = per["phone-landscape"]["overflowX"]
        assert got <= LANDSCAPE_OVERFLOW_FLOOR, (
            f"{path}: landscape overflow {got}px exceeds the measured floor "
            f"{LANDSCAPE_OVERFLOW_FLOOR}px (nav logout form); fix it, do not grow it")


def test_probe_covers_every_page_of_the_census(probe):
    pages = {p["path"] for p in SPEC["pages"] if p.get("auth", True)}
    assert set(probe) == pages


def test_the_models_menu_is_a_phone_sheet(ui_server):
    """The menu-bar pilot's acceptance (owner, 6.14.1zf): at 390px the
    Models button opens a sheet -- rows >= 44px, the panel inside the
    viewport (it ENDS ON SCREEN, the owner's sheet law), and the nav row's
    overflow-0 gate is untouched while the panel is open. Chromium is the
    judge: the panel's visibility is computed CSS, and this harness's own
    opening proves the JS."""
    from playwright.sync_api import sync_playwright
    base, cookie = ui_server["base"], ui_server["cookie"]
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        name, value = cookie.split("=", 1)
        ctx.add_cookies([{"name": name, "value": value, "url": base}])
        page = ctx.new_page()
        page.goto(f"{base}/", wait_until="networkidle")
        page.wait_for_timeout(400)
        page.click('.nav-menu-btn[data-menu="/models"]')
        page.wait_for_timeout(200)
        m = page.evaluate("""() => {
          const sub = document.querySelector('#nav-sub-models');
          const rows = [...sub.querySelectorAll('a')].map(a => {
            const r = a.getBoundingClientRect();
            return {min: Math.round(Math.min(r.width, r.height)),
                    right: Math.round(r.right), bottom: Math.round(r.bottom)};
          });
          const nav = document.querySelector('nav');
          return {hidden: sub.hidden,
                  expanded: document.querySelector('.nav-menu-btn').getAttribute('aria-expanded'),
                  rows, navOverflow: nav.scrollWidth - nav.clientWidth, vw: innerWidth};
        }""")
        assert not m["hidden"], "the menu did not open on tap"
        assert m["expanded"] == "true", "aria-expanded did not follow the open state"
        assert m["navOverflow"] <= 0, "the open menu pushed the nav row past its gate"
        for row in m["rows"]:
            assert row["min"] >= 44, f"menu row under 44px: {row}"
            assert row["right"] <= m["vw"], f"menu row passes the viewport edge: {row}"
            assert row["bottom"] <= 844, f"menu row runs off the bottom of the screen: {row}"
        # the phone scheme ruling (6.14.1zf): the in-page tab strip is gone
        # at phone width -- the sheet is the navigator, not a strip + sheet
        page.goto(f"{base}/models", wait_until="networkidle")
        page.wait_for_timeout(400)
        strip = page.evaluate("""() => {
          const s = document.querySelector('.model-tabs');
          const r = s.getBoundingClientRect();
          return {w: Math.round(r.width), h: Math.round(r.height)};
        }""")
        assert strip["h"] < 2 and strip["w"] < 2, \
            f"the models tab strip is showing at 390px: {strip} -- menu-only on phones"
        # and the sheet marks the section you are standing on
        page.goto(f"{base}/models?tab=comfy", wait_until="networkidle")
        page.wait_for_timeout(400)
        page.click('.nav-menu-btn[data-menu="/models"]')
        page.wait_for_timeout(150)
        assert page.evaluate("""() => {
          const a = document.querySelector('#nav-sub-models a[href=\"/models?tab=comfy\"]');
          return a.getAttribute('aria-current') === 'page';
        }"""), "the sheet did not mark the current section"

        # Escape closes it (the owner's law: the user always owns the exit)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert page.evaluate("() => document.querySelector('#nav-sub-models').hidden"), \
            "Escape did not close the menu"
        b.close()


def test_nav_is_a_phone_bar_everywhere(probe):
    """The nav icons round's acceptance gates (owner, 6.14.1ze): at 390px
    portrait the nav row itself must overflow 0, stay inside the viewport,
    and every nav target -- all seven links and logout -- must meet the
    44px Apple HIG tap minimum. The words hide below 900px; the glyph and
    aria-label carry the page, and the plate marks the active one."""
    for path, per in probe.items():
        navm = per["phone-portrait"]["nav"]
        assert navm, f"{path}: probe found no nav"
        assert navm["overflowX"] <= 0, f"{path}: nav overflows by {navm['overflowX']}"
        assert navm["rightEdge"] <= 0, f"{path}: nav right edge passes the viewport"
        for tgt in navm["targets"]:
            assert min(tgt["w"], tgt["h"]) >= 44, \
                f"{path}: nav target {tgt['sel']} measures {tgt['w']}x{tgt['h']}"


def test_probe_report_is_readable_facts(tmp_path, probe):
    """The report is the deliverable: numbers per page per viewport, in a
    form the owner and partner can read on a phone."""
    from tools.viewport_probe import VIEWPORTS, markdown

    md = markdown({"viewports": {k: list(v) for k, v in VIEWPORTS.items()},
                   "pages": probe})
    for needle in ("phone-portrait", "tap targets", "iOS focus-zoom", "overflow"):
        assert needle in md
    (tmp_path / "report.md").write_text(md)


def test_documented_findings_2026_11_02_are_current_findings(probe):
    """The findings are part of the record: if a design round silently
    changes one, the record has to change with it (the probe caught the
    nav-overflow, tap-target, and iOS-zoom classes; these assertions keep
    the enumerated list honest, not the page frozen)."""
    home = probe["/"]["phone-portrait"]
    # The 2026-11-02 nav findings are retired (journal 6.14.1ze): the icon
    # nav fits portrait with zero nav overflow and its targets are pinned
    # >= 44px by test_nav_is_a_phone_bar_everywhere. Page-level portrait
    # overflow and sub-24 targets remain open findings below.
    assert home["nav"]["overflowX"] <= 0, \
        "the nav icons round regressed: nav overflows portrait again"
    assert home["tap"]["under24"], \
        "sub-24px tap targets measured gone: retire the finding in the journal"
