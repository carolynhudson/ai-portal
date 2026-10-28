"""The Status page hero: what the router is doing, at a glance.

The hero used to be a wall of near-identical log lines. It is now a slot board
fed by ``app/core/rlog.py`` — these tests pin the parts a person reads:

* one row per slot, in the state the log says it is in
* a model swap announced loudly, because it looks like a hang and isn't
* the 3-second timing chatter never rendering in the log pane
* service state and controls still there (they are the reason the pane exists)

The page is rendered from a canned ``/api/router`` snapshot whose pulse comes
from running the real parser over real journal lines, so template and parser
are tested together.
"""
from __future__ import annotations

import os
import pathlib
import re
import tempfile
import time

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-hero-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import router  # noqa: E402
from app.main import app as portal_app  # noqa: E402
from app.core import rlog  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "router_journal.log"
PASSWORD = "test-password-123"


def pulse_of(lines, **kw):
    kw.setdefault("window_s", 7200)
    return rlog.parse(lines, **kw)


def install(pulse: dict, **extra):
    """Serve ``pulse`` from /api/router without touching this machine's journal.

    ``snapshot()`` is TTL-cached off its own clock, so a future timestamp makes
    the cache fresh for the duration of a test.
    """
    base = {
        "host": "http://127.0.0.1:8080", "loopback_only": True,
        "bind": {"mode": "loopback", "host": "127.0.0.1", "port": 8080},
        "web_ui": "http://127.0.0.1:8080/",
        "service": {"name": "ai-llama-router", "state": "active", "sub": "running",
                    "since": "Sat 2026-09-12 09:00:00 -0700"},
        "web": {"ok": True, "ms": 4}, "api": {"ok": True, "count": 16, "ids": [], "loaded": None},
        "loaded": pulse["models"]["loaded"],
        "tps": {"latest": None, "recent": []},
        "totals": {"prompt_tokens": None, "generated_tokens": None, "enabled": False},
        "completions": [], "memory": None, "activity": [],
    }
    router._cache = (time.time() + 3600, dict(base, pulse=pulse, **extra))


@pytest.fixture(autouse=True)
def _fresh_page(monkeypatch):
    """No HTTP from the page render: every probe stays inside the test."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("hero render must not make outbound requests")))
    yield
    router._cache = (0.0, {})


def client() -> TestClient:
    c = TestClient(portal_app)
    c.post("/login", data={"password": PASSWORD})
    return c


def hero(html: str) -> str:
    """Just the router card, so ComfyUI's pane can't satisfy assertions for it."""
    start = html.index('<div class="card router-hero">')
    rest = html[start + len('<div class="card router-hero">'):]
    end = rest.index('<div class="card router-hero">')
    return html[start:start + len('<div class="card router-hero">') + end]


# --------------------------------------------------------------- the API shape

def test_api_router_carries_the_pulse():
    install(pulse_of(FIXTURE.read_text().splitlines()))
    d = client().get("/api/router").json()
    p = d["pulse"]
    for key in ("slots", "series", "models", "attention", "stats", "active_slots",
                "slot_ids"):
        assert key in p, f"/api/router pulse is missing {key}"
    assert p["slots"], "real journal lines should report slots"
    assert p["stats"]["window_s"] == 7200


def test_the_polled_payload_leaves_the_raw_journal_out():
    """The hero polls this every couple of seconds; 60 log lines are most of the
    bytes and the pane that reads them is off by default (§6.9)."""
    install(pulse_of(FIXTURE.read_text().splitlines()),
            activity=[{"kind": "log", "text": f"[55601] 905.{i}.000.000 I slot print_timing: "
                       f"id  1 | task 5 | n_gen =  {i}, tg =   6.00 t/s"}
                      for i in range(60)])
    c = client()
    lean = c.get("/api/router").json()
    assert "pulse" in lean and "activity" not in lean
    full = c.get("/api/router?raw=1").json()
    assert len(full["activity"]) == 60
    import json
    assert len(json.dumps(lean)) + 5000 < len(json.dumps(full)), \
        "the polled payload is carrying the raw tail again"


# ---------------------------------------------------------------- the lane board

def test_every_slot_the_log_saw_gets_a_row_in_its_state():
    p = pulse_of(FIXTURE.read_text().splitlines())
    install(p)
    html = client().get("/").text
    body = hero(html)
    for s in p["slots"]:
        assert f'data-slot="{s["id"]}"' in body, f"no lane row for slot {s['id']}"
        assert f'class="lane s-{s["state"]}' in body, f"slot {s['id']} rendered as the wrong state"
    assert 'id="pulse-lanes"' in body


def test_a_generating_slot_shows_tokens_and_speed():
    install(pulse_of([
        "[55601] 905.10.000.000 I slot launch_slot_: id  1 | task 500 | processing task, is_child = 0",
        "[55601] 905.20.000.000 I slot print_timing: id  1 | task 500 | n_gen =  512, tg =   6.10 t/s, tg_3s =   6.05 t/s",
    ]))
    body = hero(client().get("/").text)
    assert "lane s-generate" in body
    assert "generating" in body
    assert "6.1 t/s" in body or "6.10 t/s" in body


def test_a_prompt_being_read_shows_its_real_percentage():
    install(pulse_of([
        "[55601] 905.10.000.000 I slot launch_slot_: id  2 | task 600 | processing task, is_child = 0",
        "[55601] 905.13.000.000 I slot print_timing: id  2 | task 600 | prompt processing, "
        "n_tokens = 1800, progress = 0.45, t = 3.10 s / 580.00 tokens per second",
    ]))
    body = hero(client().get("/").text)
    assert "lane s-prompt" in body
    assert "45%" in body            # width of the bar, from llama.cpp's own progress
    assert "reading prompt" in body


def test_an_idle_log_says_so_instead_of_rendering_an_empty_board():
    install(pulse_of([]))
    body = hero(client().get("/").text)
    assert 'id="lanes-none"' in body
    assert "no requests in the log window" in body


# ---------------------------------------------------------------- throughput chart
# jsdom has no canvas, so the harness can only see that <canvas> exists. The
# drawing function is pure arithmetic over the pulse's bins, so it can be pulled
# out of the template and run against a stub context — which is the only way
# this pane gets any test at all.

DRIVER = r"""
import { writeFileSync } from "fs";
const calls = [];
const ctx = new Proxy({}, {
  get: (o, k) => (typeof o[k] !== "undefined" ? o[k] : (...a) => { calls.push([k, a]); }),
  set: (o, k, v) => { calls.push([k, v]); o[k] = v; return true; },
});
const tpsCv = { width: 560, height: 52 };
const tpsCx = ctx;
const SLOT_COLORS = ["#5aa9ff", "#f0883e"];
%SOURCE%

const say = (label, ok) => console.log(label + " " + (ok ? "yes" : "no"));

// the calls between a strokeStyle set and the path before it are that slot's
// stroked line — the only way to see one slot's polyline through the proxy
const segFor = (color) => {
  let end = -1;
  for (let i = calls.length - 1; i >= 0; i--)
    if (calls[i][0] === "strokeStyle" && calls[i][1] === color) { end = i; break; }
  const pts = [];
  for (let i = end - 1; i >= 0 && (calls[i][0] === "moveTo" || calls[i][0] === "lineTo"); i--)
    pts.push(calls[i][1]);
  return pts.reverse();
};

// 50 seconds of samples, then silence: the area must stop at the last real
// sample and the gap must be named, not drawn as a drop to zero
drawPulse({ series: { bins: Array.from({ length: 10 }, (_, i) => ({
      t: 1000 + i * 5, per: i < 7 ? { "1": 6, "3": 2 } : {} })),
    bin_s: 5, window_s: 420 }, time: { now: 1000 + 13 * 5 } });
const pts = calls.filter(c => c[0] === "moveTo" || c[0] === "lineTo");
say("draws-areas", pts.length > 10);
say("reaches-right-edge", Math.max(...pts.map(c => c[1][0])) > 500);
const labels = calls.filter(c => c[0] === "fillText").map(c => c[1][0]);
say("names-the-silence", labels.some(t => /last sample \d+s ago/.test(t)));
say("labels-peak", labels.some(t => /^\d+ t\/s$/.test(t)));

// an empty window says so instead of drawing a flat line pretending to be data
calls.length = 0;
drawPulse({ series: { bins: [], bin_s: 5, window_s: 420 }, time: { now: 1 } });
say("empty-says-so", calls.filter(c => c[0] === "fillText")
  .map(c => c[1][0]).some(t => /no token activity/.test(t)));

// all-empty bins must not divide by zero or throw
drawPulse({ series: { bins: [{ t: 1, per: {} }], bin_s: 5 }, time: { now: 99 } });

// a slot that stops mid-window draws its zeros (falls onto the stack below
// it), never a gap: slot 3 generates in the first bins then goes quiet
calls.length = 0;
drawPulse({ series: { bins: Array.from({ length: 9 }, (_, i) => ({
      t: 100 + i * 5, per: i <= 4 ? { "1": 6, "3": 4 } : { "1": 6 } })),
    bin_s: 5, window_s: 60 }, time: { now: 140 } });
const seg3 = segFor("#f0883e");
// stacked band: slot 3's top sits at 10 t/s while it generates, at slot 1's
// 6 t/s once idle — so "zero rate" means its y lands on the 6-line and stays
// (the chart's scale floor is 20 t/s, so the y's follow from that)
const yBusy = 52 - (10 / 20) * 44 - 2;
const yIdle = 52 - (6 / 20) * 44 - 2;
const late3 = seg3.filter(p => p[0] >= 420);
say("zero-fills-idle-slot",
    late3.length >= 3 && seg3.some(p => Math.abs(p[1] - yBusy) < 0.01)
    && late3.every(p => Math.abs(p[1] - yIdle) < 0.01));

// an in-flight slot whose newest rate is not in a bin yet carries its last
// rate to the right edge as a dashed tail (not a cliff, not a fabricated bin)
calls.length = 0;
drawPulse({ series: { bins: Array.from({ length: 7 }, (_, i) => ({
      t: 1000 + i * 5, per: { "1": 8 } })), bin_s: 5, window_s: 420 },
    time: { now: 1034 },
    slots: [{ id: 1, state: "generate", age_s: 2, gen_tps: 8 }] });
const seg1 = segFor("#5aa9ff");
say("carries-active-rate-to-edge",
    Math.max(...seg1.map(p => p[0])) === 560
    && calls.some(c => c[0] === "setLineDash" && c[1][0] && c[1][0][0] === 4));

// the window is a sliding TIME window: samples older than the window are
// gone from the drawing entirely — nothing is plotted left of x=0
calls.length = 0;
drawPulse({ series: { bins: [{ t: 10, per: { "1": 9 } }, { t: 20, per: { "1": 9 } }],
    bin_s: 5, window_s: 420 }, time: { now: 5000 } });
const pts2 = calls.filter(c => c[0] === "moveTo" || c[0] === "lineTo").map(c => c[1]);
say("culls-old-points", pts2.every(p => p[0] >= 0 && p[0] <= 560)
    && calls.filter(c => c[0] === "fillText").map(c => c[1][0])
         .some(t => /no token activity/.test(t)));

// the line enters from OFF the left edge and the canvas clips it: the oldest
// point scrolls out of view instead of leaving a stub that grows and pops
// when it falls off the window (the owner's "gap that shrinks then pops")
calls.length = 0;
drawPulse({ series: { bins: Array.from({ length: 8 }, (_, i) => ({
      t: 995 + i * 5, per: { "1": 7 } })), bin_s: 5, window_s: 420 },
    time: { now: 1032 } });
const ptsL = calls.filter(c => c[0] === "moveTo" || c[0] === "lineTo").map(c => c[1]);
say("extends-past-left-edge",
    ptsL.some(p => p[0] < -1) && calls.some(c => c[0] === "clip"));

// the chart is more than one metric: prompt rates ride the same grid on
// their own scale (prefill is ~20x generation speed), and router events are
// little shapes on the time axis, at the time they happened
calls.length = 0;
drawPulse({ series: { bins: Array.from({ length: 8 }, (_, i) => ({
      t: 995 + i * 5, per: i > 2 ? { "1": 7 } : {},
      pr: i > 2 ? {} : { "1": 800 } })), bin_s: 5, window_s: 420 },
    time: { now: 1032 },
    attention: [
      { level: "warn", text: "cache pressure", age_s: 20 },
      { level: "error", text: "out of memory", age_s: 5000 }
    ] });
const rects = calls.filter(c => c[0] === "fillRect").map(c => c[1]);
const labelsE = calls.filter(c => c[0] === "fillText").map(c => c[1][0]);
say("marks-events", rects.length === 1
    && rects[0][0] > 0 && rects[0][0] < 560
    && calls.some(c => c[0] === "fillStyle" && c[1] === "#d29922"));
say("draws-prompt-band", calls.some(c => c[0] === "strokeStyle" && c[1] === "#a5d6ff")
    && labelsE.some(t => /prompt 800 t\/s/.test(t)));
say("labels-both-scales", labelsE.some(t => /^\d+ t\/s$/.test(t))
    && labelsE.some(t => /own scale/.test(t)));
say("labels-the-axis", labelsE.some(t => /-7:00/.test(t))
    && labelsE.some(t => /now/.test(t)));

console.log("survived");
"""


def test_throughput_chart_draws_what_the_bins_say():
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the chart code")
    template = pathlib.Path(__file__).resolve().parents[1] / "web" / "templates" / "dashboard.html"
    text = template.read_text()
    start = text.find("function drawPulse(pulse){")
    assert start >= 0, "drawPulse disappeared from the hero — update this test"
    end = text.index("\n}\n", start) + 3
    source = text[start:end]
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(DRIVER.replace("%SOURCE%", source))
        script = fh.name
    out = subprocess.run([node, script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    lines = dict(line.split(" ", 1) for line in out.stdout.strip().splitlines()
                 if " " in line)
    for behaviour in ("draws-areas", "reaches-right-edge", "names-the-silence",
                      "labels-peak", "empty-says-so", "zero-fills-idle-slot",
                      "carries-active-rate-to-edge", "culls-old-points",
                      "extends-past-left-edge", "marks-events",
                      "draws-prompt-band", "labels-both-scales",
                      "labels-the-axis"):
        assert lines.get(behaviour) == "yes", f"{behaviour}: {out.stdout}"
    assert "survived" in out.stdout


def test_the_chart_row_is_only_the_chart():
    """The generating number must not live beside the sparkline: its digit
    width shifting the canvas is exactly what kills the sense of motion, so
    the number lives in the totals row and the chart row draws alone."""
    template = pathlib.Path(__file__).resolve().parents[1] / "web" / "templates" / "dashboard.html"
    text = template.read_text()
    row = text[text.index('<div class="tps-row">'):text.index('<div class="pulse-signals"')]
    assert 'id="tps"' in row, "the sparkline left its row"
    assert "tps-big" not in row and "tps-now" not in row,         "a number in the chart row resizes the chart as its digits change"
    assert text.count('id="tps-now"') == 1
    assert 'id="tps-now"' in text[text.index('<div class="totals-row">'):],         "the generating readout has to live somewhere readable"
    # the sliding-time-window machinery, pinned as the source it is: a client
    # clock anchored to each payload, a grid that starts at the window edge
    # (cull by construction), the dashed carry-forward, and the redraw loop
    # that slides the window between polls
    src = text[text.index("function drawPulse(pulse){"):]
    for marker in ("series.window_s", "PULSE_POLL", "setLineDash",
                   "Math.floor(left / binS)", "tpsCx.clip()", "PULSE_KEEP",
                   "pulseForChart", "prPeak", "fillRect", "a5d6ff"):
        assert marker in src, f"drawPulse lost {marker!r}"
    assert "every(1000, () => { if (PULSE_KEEP) drawPulse(PULSE_KEEP); });" in text, \
        "the between-poll redraw is what makes the window slide; it must run"


# ------------------------------------------------------------- model switching

def test_a_model_switch_is_announced_with_the_model_name():
    install(pulse_of([
        "[55601] 900.10.000.000 I srv  proxy_reques: proxying request to model Mistral-Code-24B on port 55601",
        "[55601] 900.11.000.000 I model name=Mistral-Code-24B is not loaded, loading...",
        "[55601] 900.40.000.000 I waiting until model name=Mistral-Code-24B is fully loaded...",
    ]))
    body = hero(client().get("/").text)
    tag = re.search(r'<div class="switch-line" id="pulse-switch"([^>]*)>', body)
    assert tag, "the switch line is missing from the hero"
    assert "hidden" not in tag.group(1), "a model swap in progress must be visible"
    assert "Mistral-Code-24B" in body
    assert "waiting for a free slot to load" in body
    assert "pm-dot load" in body


def test_the_switch_line_is_hidden_while_things_are_normal():
    install(pulse_of(FIXTURE.read_text().splitlines()))
    body = hero(client().get("/").text)
    tag = re.search(r'<div class="switch-line" id="pulse-switch"([^>]*)>', body)
    assert tag and "hidden" in tag.group(1)


# ------------------------------------------------------------------- the log pane

def test_the_timing_chatter_never_renders_in_the_attention_pane():
    install(pulse_of(FIXTURE.read_text().splitlines()))
    body = hero(client().get("/").text)
    pane = body.split('id="router-feed"')[1]
    assert "n_gen" not in pane
    assert "graphs reused" not in pane
    assert "print_timing" not in pane


def test_the_pane_leads_with_curated_lines_and_offers_the_raw_tail():
    install(pulse_of(FIXTURE.read_text().splitlines()))
    body = hero(client().get("/").text)
    toggle = body.split('id="attention-toggle"')[1].split(">", 1)[1].split("<")[0]
    assert "all lines" in toggle, "default view is attention, so the button offers the other one"
    pane = body.split('id="router-feed"')[1]
    assert "Attention ·" in body
    assert any(needle in pane for needle in
               ("prompt cache pressure", "context limit", "http client error",
                "position mismatch", "cancelled")), pane[:400]


# --------------------------------------------------- the pane's original purpose

def test_service_state_and_controls_survive_the_redesign():
    install(pulse_of(FIXTURE.read_text().splitlines()))
    body = hero(client().get("/").text)
    for needle in ('id="router-state"', "svcAction('llamacpp', 'start')",
                   "svcAction('llamacpp', 'stop')", "svcAction('llamacpp', 'restart')",
                   "svc: ai-llama-router", "loopback only", "Web UI ↗", "/v1/models"):
        assert needle in body, f"hero lost {needle}"
