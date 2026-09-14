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
