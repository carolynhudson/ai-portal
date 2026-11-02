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
  * landscape 844px overflow has not grown past its measured 42px (the nav
    logout form; a design round owns it, and regressions past the floor fail);
  * the probe covers every authed page of the UI census.

Enumerated, not pinned (the 2026-11-02 findings, from ~/ai/logs/viewport-20261102):
  * portrait horizontal overflow ~496px at 390px - the nav (brand + 7 links
    + resource lights + logout) is a 886px flex row that never wraps;
  * nav links measure 23px tall (Apple HIG minimum is 44);
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
# the measured 2026-11-02 landscape overflow (nav logout form). A design
# round may lower it; growing it is a regression and fails.
LANDSCAPE_OVERFLOW_FLOOR = 42


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
    assert home["overflowX"] >= 400, \
        "portrait nav overflow measured fixed: retire the finding in the journal"
    assert home["tap"]["under24"], \
        "sub-24px tap targets measured gone: retire the finding in the journal"
