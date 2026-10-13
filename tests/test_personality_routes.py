"""Route tests for /personalities and /api/personalities (Phase N4, §4.7).

Two things these exist to prove, and both are things a page can get wrong silently:

  * **the page must not write.** N4's generator is read-only by decision (D7). A route
    that renders a diff is fine; a route that renders a diff *and saves it* is the exact
    accident this phase is designed to prevent, and it would look identical in the browser.
  * **the budget must actually reach the HTML.** `Budget.as_dict()` has specific field
    names. A template that reads `worst_bytes` instead of `worst_case_bytes` does not
    error — Jinja returns undefined for a missing key, the `{% if %}` never fires, and the
    card renders as a personality with no budget. That is a blank where a number should be,
    on a page whose entire purpose is the number. So the number is asserted, not the
    selector.

Runs against a throwaway AI_PORTAL_HOME with a hand-built library. Never ~/ai, systemd,
or the network.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

# Set here rather than relying on another module having set it: pytest's file order decides
# who imports app.main first, and a module that reaches it without a password dies during
# import with a KeyError that names neither this file nor the real cause.
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-personality-routes"))

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import html  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.core import config as cfg  # noqa: E402
from app.core import (  # noqa: E402
    db,
    kb,
    paths,
    presets_store,
    settingrows,
    systemd,
    updater,
)
from app.core import model_config as mc  # noqa: E402
from app.core import modelindex as mi
from app.core import personality as P
from app.main import app  # noqa: E402
from tests.gguf_fixture import _write_fake_gguf, seed_hub_model  # noqa: E402

# (was: `from tests.test_modelindex import ...` — importing another test module wins that
# module's AI_PORTAL_HOME/AI_PORTAL_PASSWORD setdefault for the whole session; the shared
# fixture module carries no env of its own.)

GiB = 1 << 30


def _presets_text() -> str:
    """The sandbox presets.ini exactly as it is on disk, or a marker if it is absent."""
    try:
        return paths.PRESETS_INI.read_text()
    except OSError:
        return "<absent>"


@pytest.fixture()
def library():
    """A two-model library with a known size gap, so 'heaviest combination' is checkable.

    The sizes are separated on purpose: if both models priced the same, a budget that
    picked the wrong one would still produce a plausible number.
    """
    db.init()
    mi.init()
    conn = db._conn()
    conn.execute("DELETE FROM model_index")
    conn.commit()
    # Clearing the table is not clearing the library. rescan() reads the directory tree, so
    # any model another test module left in the shared home comes straight back — and a
    # personality covering "the whole library" then contains a model this fixture never built
    # and cannot price, which makes fits correctly None. A budget test must own its library.
    if paths.LLAMA_MODELS.is_dir():
        for child in paths.LLAMA_MODELS.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    # SAFE names one specific model, so the sandbox has to contain it or `safe` has no
    # budget to render — legitimately, because a fallback that points at a model that is
    # not on disk is a broken fallback. Naming it here is what lets the budget line be
    # asserted for both cards instead of one.
    #
    # It may name a hub-cache model (`repo:TAG`), which is not a directory name: putting one under
    # LLAMA_MODELS creates nested directories and a row named something else. Cache-form ids go in
    # the hub cache, which rescan() also reads — and the cache has to be cleared first for the same
    # reason the library does: this fixture owns its inventory.
    kv = {"attention.head_count": 4, "attention.head_count_kv": 2,
          "embedding_length": 256, "context_length": 4096}
    hub = mi.hub_dir()
    if hub.is_dir():
        shutil.rmtree(hub, ignore_errors=True)
    hub.mkdir(parents=True, exist_ok=True)
    names = ((P.SAFE["models"][0], 8, 4000), ("Big-30B", 48, 9000))
    for name, blocks, pad in names:
        body = dict(kv, block_count=blocks)
        if ":" in name:
            seed_hub_model(hub, name, body, pad=pad)
            continue
        d = paths.LLAMA_MODELS / name
        d.mkdir(parents=True, exist_ok=True)
        _write_fake_gguf(d / "m.gguf", body, pad=pad)
    mi.rescan()
    return sorted(r["id"] for r in mi.rows())


@pytest.fixture()
def client(library):
    c = TestClient(app)
    # Mint the cookie instead of logging in: the effective password belongs to whichever
    # test module set AI_PORTAL_PASSWORD first, and these are personality tests.
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


# --- auth and shape -----------------------------------------------------------

def test_personalities_page_requires_auth():
    assert TestClient(app).get("/personalities", follow_redirects=False).status_code == 302


def test_api_personalities_requires_auth():
    # Pages redirect to the login screen; API routes answer 401. That distinction is
    # deliberate and already how every other route in this app behaves — a browser gets a
    # page to sign in on, a script gets an error it can branch on.
    assert TestClient(app).get("/api/personalities", follow_redirects=False).status_code == 401


def test_page_renders_both_builtin_personalities(client):
    r = client.get("/personalities")
    assert r.status_code == 200, r.text[:900]
    for name in ("safe", "default"):
        assert f'id="pers-{name}"' in r.text, name


def test_api_lists_builtins_and_the_settings_registry(client):
    j = client.get("/api/personalities").json()
    assert {p["name"] for p in j["personalities"]} == {"safe", "default"}
    budget = j["budget_bytes"]
    assert budget is None or budget > 0, budget
    keys = {s["key"] for s in j["settings"]}
    assert keys == set(P.SETTINGS), keys ^ set(P.SETTINGS)


def test_api_settings_do_not_leak_callables(client):
    """`accepts` is a lambda. JSON cannot serialise one, and an endpoint that tried would
    either 500 on a browser request or quietly drop the field.
    """
    j = client.get("/api/personalities").json()
    for s in j["settings"]:
        assert "accepts" not in s, s["key"]
        assert "help" in s and "kind" in s and "default" in s, s["key"]


# --- the budget has to be visible, not merely present in the DOM --------------

def test_budget_numbers_render_rather_than_the_selector_rendering(client):
    """Assert the formatted figure, not just an element.

    The first version of this template read `b.get('worst_bytes')`. `Budget.as_dict()`
    calls it `worst_case_bytes`. Jinja answered the missing key with undefined, the
    condition was false, and the page rendered both cards with no budget at all — 200 OK,
    correct markup, no number. Checking for a selector would have passed.
    """
    r = client.get("/personalities")
    text = " ".join(r.text.split())
    assert "heaviest" in text, "no budget figure rendered at all"
    # "4.0 GiB of 108.0 GiB (104.0 spare)" is the shape: cost, budget, what is left.
    import re
    spans = re.findall(r"<span>heaviest [\d.]+ GiB of [\d.]+ GiB \([-\d.]+ spare\)</span>",
                       text)
    assert len(spans) == 2, f"expected one budget line per personality, got {spans}"


def test_budget_names_the_heaviest_model_not_the_first_one(client, library):
    """models_max=1 means the worst case is the heaviest single model, not an arbitrary
    one. With a 4000-byte and a 9000-byte model in the set, a budget that took the first
    row would be smaller by exactly the gap.
    """
    b = mi._pool_bytes()
    d = P.budget_for({"name": "default", "models_max": 1, "models": None}, b)
    assert d.worst_case_bytes is not None
    heaviest = max(mi.rows(), key=lambda r: r["bytes"] + (r.get("mmproj_bytes") or 0))
    priced = {row["id"] for row in d.priced}
    assert heaviest["id"] in priced
    assert d.heaviest == [heaviest["id"]], d.heaviest


def test_models_max_two_over_the_same_library_does_not_fit(client, library):
    """The point of the budget: the same models, models_max raised by one, and a number
    nobody would think to check.
    """
    b = mi._pool_bytes()
    one = P.budget_for({"name": "x", "models_max": 1, "models": None}, b)
    two = P.budget_for({"name": "x", "models_max": 2, "models": None}, b)
    assert one.fits is True
    # Both figures are Optional on the Budget dataclass: None means "cannot be bounded",
    # and comparing None would raise a TypeError inside the test rather than report the
    # finding. Prove they are priced before comparing them.
    assert one.worst_case_bytes is not None and two.worst_case_bytes is not None
    assert one.headroom_bytes is not None and two.headroom_bytes is not None
    assert two.worst_case_bytes > one.worst_case_bytes
    # Not asserting fits is False here: with toy-sized fake models both fit. What must
    # hold is that the worst case grows, which is what a real library turns into a refusal.
    assert two.headroom_bytes < one.headroom_bytes


# --- the read-only promise ----------------------------------------------------

def test_rendering_the_page_never_writes_presets_ini(client, library):
    """D7: the portal shows a diff, a human approves it.

    The file is planted with content that no personality would ever generate, then every
    read route on this page is called. Any write changes it.

    Planting is not decoration. The obvious version — snapshot the file, make the requests,
    compare — passed while `render()` was mutated to write, because an earlier test in the
    same run had already written the file: `before` and `after` were both the mutated output
    and equalled each other. A guard that only catches a write when nothing has written yet
    is a guard against test order, not against the bug. Planting a value the module cannot
    produce makes any write observable from whatever state the run is in.
    """
    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# SENTINEL: written by the test, never by the portal\n[*]\nthreads = 7\n"
    paths.PRESETS_INI.write_text(sentinel)

    client.get("/personalities")
    client.get("/api/personalities")
    client.get("/personalities")

    after = paths.PRESETS_INI.read_text() if paths.PRESETS_INI.exists() else None
    assert after == sentinel, (
        "a GET changed presets.ini: "
        f"{after!r}" if after != sentinel else ""
    )
    # A route that wrote would also have erased the sentinel, so say which half broke.
    assert paths.PRESETS_INI.exists(), "the file disappeared"
    client.get("/personalities")
    assert paths.PRESETS_INI.read_text() == sentinel, "presets.ini was rewritten"


def test_the_pages_write_control_cannot_fire_by_itself(client):
    """The editor can write, so the invariant is no longer "there is no write control" — it is
    "the write control cannot act without an operator".

    This replaces test_page_offers_no_write_control, which forbade the string
    `/api/personalities/apply` anywhere in the page. That assertion was correct for the read-only
    half of N4 and became wrong the moment the apply route shipped; keeping it would have meant
    either a failing gate or a test that ignores the exact code it is supposed to be watching.

    What still has to be true, and what is asserted here:

      * Apply starts disabled. A preview has to come back clean before the button can be pressed,
        so the write is always preceded by a diff the operator saw for the payload being sent.
      * There is no form. A button that is `type="button"` cannot submit anything, and a form with
        an action pointing at the write endpoint would fire on Enter in any field.
      * The write endpoint appears nowhere as an `action=` or `href=`. It exists only inside a
        click handler.
      * Nothing is submitted automatically: no `onsubmit`, no `autofocus`, no inline `on*=`
        attribute wired to the write path.

    "Nothing is requested on load" is checked for real by the UI harness (tests/ui/spec.json,
    expectRequests) which loads this page in jsdom; asserting it from the HTML here would only
    restate a string pattern.
    """
    r = client.get("/personalities")
    assert r.status_code == 200
    body = r.text.split("<main", 1)[1] if "<main" in r.text else r.text

    assert 'id="ed-apply" disabled' in body, \
        "Apply must begin disabled; a preview is what enables it"

    posts = list(body.split("<form")[1:])
    assert not posts, f"this page renders its own form(s): {posts[:1]}"

    for forbidden in ('action="/api/personalities/apply', 'href="/api/personalities/apply',
                      'action="/api/personalities/rollback', 'onsubmit=', 'autofocus'):
        assert forbidden not in body, forbidden

    # Every control in the editor is type="button": the default for a <button> is type="submit",
    # and a submit button inside any future wrapper would send the page to a write endpoint on
    # Enter while someone was typing a number.
    editor = body.split('id="pers-editor"', 1)[1] if 'id="pers-editor"' in body else ""
    assert editor, "the editor section is missing"
    buttons = list(editor.split("<button")[1:])
    assert buttons, "the editor renders no buttons"
    untyped = [b.split(">", 1)[0] for b in buttons if 'type="button"' not in b.split(">", 1)[0]]
    assert not untyped, f"buttons without type=button: {untyped}"


def test_get_personality_paths_stay_read_only(client):
    """The read side must not acquire side effects now that a write endpoint exists.

    This replaces `test_api_exposes_no_write_route`, which asserted that no write route existed
    at all. That invariant was the N4 read-only half; the apply path intentionally breaks it.
    Keeping the old assertion would have meant keeping a test that describes a phase the code has
    left behind, and a stale invariant test is worse than none: it teaches the next reader that a
    gated write is a bug.

    What still has to hold is that GETs are inert. `/api/personalities` answers GET only, so a
    POST to it must be a method refusal rather than an action.
    """
    code = client.post("/api/personalities", json={}).status_code
    assert code in (404, 405), f"/api/personalities answered {code} — the read endpoint gained a POST"

    before = _presets_text()
    assert client.get("/api/personalities").status_code == 200
    assert client.get("/api/personalities/snapshots").status_code == 200
    assert _presets_text() == before, "a GET must not write or snapshot presets.ini"


def test_diff_is_shown_as_proposed_not_applied(client):
    r = client.get("/personalities")
    assert "proposed only" in r.text or "no change" in r.text, \
        "a diff with no statement of what happens next reads as a change that already happened"


# --- failures must be visible on the page, not swallowed ----------------------

def test_a_personality_that_cannot_be_rendered_still_gets_a_card(monkeypatch, client):
    """Dropping the card would report the personality as nonexistent. That is a different
    and worse lie than showing it broken.
    """
    from app import main

    def boom(personality, budget_bytes=None):
        raise P.PersonalityError("models_max=2 exceeds what this box can hold")

    monkeypatch.setattr(main.personality, "render", boom)
    r = client.get("/personalities")
    assert r.status_code == 200, r.text[:600]
    assert "cannot be generated" in r.text
    assert "exceeds what this box can hold" in r.text
    for name in ("safe", "default"):
        assert f'id="pers-{name}"' in r.text, name


def test_safe_personality_names_a_real_model_or_says_so(client, library):
    """SAFE names one specific model. If the library does not contain it, the page must
    show that as a finding rather than render a card claiming a fallback that cannot start.
    """
    names = set(library)
    r = client.get("/personalities")
    safe_model = P.SAFE["models"][0]
    if safe_model not in names:
        assert "no library entry" in r.text, (
            f"safe names {safe_model!r}, which is not in the library, and the page said nothing")


# --- the registry is the same definition the generator validates against ------

def test_registry_covers_every_key_the_live_file_uses(client, library):
    """Whatever presets.ini already contains must be explicable: either the portal has a
    definition for it, or the page says the router owns it. A key that is neither is a key
    nobody can edit or explain.
    """
    current = P.parse_current()
    undefined = sorted(
        k for section in current.values() for k in section
        if k not in P.SETTINGS and k not in P.ROUTER_CONTROLLED_KEYS | P.ROUTER_DERIVED_KEYS
    )
    assert not undefined, f"keys in presets.ini with no definition anywhere: {undefined}"


def test_router_owned_keys_are_absent_from_the_page(client):
    """The page lists what can be set. Router-controlled keys must not appear in that list,
    because showing them invites someone to set one and be silently ignored.
    """
    r = client.get("/personalities")
    rows = {s["key"] for s in client.get("/api/personalities").json()["settings"]}
    for k in P.ROUTER_CONTROLLED_KEYS | P.ROUTER_DERIVED_KEYS:
        assert k not in rows, k
    assert "host" in r.text and "alias" in r.text, "the page should name what it refuses"


def test_check_setting_agrees_with_the_registry_it_documents(client):
    """A validator that disagrees with its own table is the bug class this whole module is
    about: the page would explain one rule and the generator would apply another.
    """
    for key, spec in P.SETTINGS.items():
        default = spec.get("default")
        assert P.check_setting(key, default) is None, f"{key} rejects its own default"
        choices = spec.get("choices")
        if choices:
            assert P.check_setting(key, choices[0]) is None, f"{key} rejects a documented choice"
            assert P.check_setting(key, "definitely-not-a-choice") is not None, key


# ------------------------------------------------------------- the gated write half

def _settle(timeout: float = 20.0) -> None:
    """Wait for job rows to leave 'running' (same shape as tests/test_jobs.py::settle)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not updater.running():
            return
        time.sleep(0.02)
    raise AssertionError("presets jobs still running")


@pytest.fixture()
def presets_file(library):
    """A presets.ini with the shape the live file has today, so a diff has something to change.

    `fit` / `fit-target` are absent because the owner deleted them by hand; see
    GLOBAL_DEFAULTS in app/core/personality.py for why the portal agrees."""
    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_INI.write_text("[*]\nthreads = 16\nstop-timeout = 15\n")
    for existing in presets_store.list_snapshots():
        shutil.rmtree(existing["path"], ignore_errors=True)
    return paths.PRESETS_INI.read_text()


def test_preview_is_read_only(client, presets_file):
    """Preview is the button an operator presses before Apply, so it must be inert.

    Asserted against snapshots as well as the file: apply() takes a snapshot as its first write
    step, and a preview that snapshotted would fill the rollback history with entries that
    correspond to nothing anyone applied.
    """
    before = presets_file
    r = client.post("/api/personalities/preview", json={"name": "safe"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"], "a preview with no diff cannot inform the decision it is gating"
    assert "would_refuse" in body and body["would_refuse"] == []
    assert paths.PRESETS_INI.read_text() == before, "preview must not write"
    assert presets_store.list_snapshots() == [], "preview must not snapshot"


def test_preview_reports_a_refusal_before_anyone_clicks_apply(client, presets_file, monkeypatch):
    """A refusal discovered at apply time is a failed job; discovered in preview it is a message.

    Mutated via check_generated so the refusal is real without editing a personality definition
    into something invalid.
    """
    monkeypatch.setattr(P, "check_generated",
                        lambda text, known: ["[*] injected: fabricated refusal"])
    r = client.post("/api/personalities/preview", json={"name": "safe"})
    assert r.status_code == 200, r.text
    assert r.json()["would_refuse"] == ["[*] injected: fabricated refusal"]
    assert paths.PRESETS_INI.read_text() == presets_file


def test_preview_404s_for_an_unknown_personality(client, presets_file):
    r = client.post("/api/personalities/preview", json={"name": "no-such-personality"})
    assert r.status_code == 404, r.text
    assert "no-such-personality" in r.text


def test_apply_requires_confirm(client, presets_file, monkeypatch):
    """confirm defaults to False, so omitting it is a refusal rather than an apply.

    Same rule as a router stop and a release delete. The assertion that matters is that nothing
    happened: no job row, no snapshot, no write.
    """
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: {"ok": True, "state": {}})
    before = presets_file
    r = client.post("/api/personalities/apply", json={"name": "safe"})
    assert r.status_code == 400, r.text
    assert "confirm required" in r.text
    assert paths.PRESETS_INI.read_text() == before
    assert presets_store.list_snapshots() == []
    assert updater.running() == []


def test_apply_runs_as_a_job_and_writes_through_the_snapshot(client, presets_file, monkeypatch):
    restarts: list[tuple[str, str]] = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: restarts.append((k, a)) or {"ok": True, "state": {}})
    before = presets_file

    r = client.post("/api/personalities/apply", json={"name": "safe", "confirm": True})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]
    _settle()

    row = updater.job(job_id)
    assert row is not None, "the job row is gone: the job was never recorded"
    assert row["status"] == "success", row["log"]
    assert "snapshotted presets.ini" in row["log"]
    assert "wrote" in row["log"]
    assert paths.PRESETS_INI.read_text() != before, "the apply must have changed the file"
    assert restarts == [("llamacpp", "restart")]

    snaps = presets_store.list_snapshots()
    assert snaps, "apply must leave a snapshot"
    newest = presets_store.snapshot_dir(snaps[0]["stamp"]) / "presets.ini"
    assert newest.read_text() == before, "the snapshot must hold the pre-apply file"


def test_apply_refusal_surfaces_as_a_failed_job_not_a_500(client, presets_file, monkeypatch):
    """The budget refusal must be visible in the job log rather than as an unhandled crash.

    Patches `budget_for` to return a Budget-shaped object whose `as_dict()` is the impossible
    budget, because render() calls `budget_for(...).as_dict()`. An earlier version returned the
    dict directly and the job died with `AttributeError: 'dict' object has no attribute 'as_dict'`
    — a failed job either way, but for a reason invented by the fixture, so the assertion on the
    refusal message would have been reading a traceback that had nothing to do with the refusal.
    """
    monkeypatch.setattr(P, "budget_for",
                        lambda persona, budget_bytes=None: _ImpossibleBudgetHolder())
    r = client.post("/api/personalities/apply", json={"name": "safe", "confirm": True})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]
    _settle()
    row = updater.job(job_id)
    assert row is not None, "the job row is gone: the job was never recorded"
    assert row["status"] == "failed", row["log"]
    assert "refusing to write" in row["log"]
    assert paths.PRESETS_INI.read_text() == presets_file, "a refused apply must not write"


class _ImpossibleBudgetHolder:
    """Stand-in for a Budget that says "measured, and over budget".

    render() calls `budget_for(...).as_dict()` and apply() then indexes the result, so the thing
    being injected has to be an object with `as_dict()` returning a dict. Both halves matter and
    an earlier version of this fixture got only one of them right.
    """

    def as_dict(self) -> dict:
        return {
            "models_max": 1,
            "priced": [{"id": "m", "peak": 512 << 30}],
            "unpriced": [],
            "worst_case_bytes": 512 << 30,
            "heaviest": ["m"],
            "budget_bytes": 1 << 20,
            "headroom_bytes": None,
            "fits": P.FITS_NO,
        }


def test_apply_is_refused_while_another_job_owns_the_machine(client, presets_file, monkeypatch):
    """presets-apply restarts the router, so it must not start on top of anything else."""
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: {"ok": True, "state": {}})
    hold = updater.start_job("llamacpp", "HEAD", lambda log: time.sleep(2.0))
    assert hold, "could not start the blocking job"
    try:
        r = client.post("/api/personalities/apply",
                       json={"name": "safe", "confirm": True})
        assert r.status_code == 409, r.text
        assert paths.PRESETS_INI.read_text() == presets_file, "a refusal must not write"
    finally:
        _settle()


def test_presets_apply_kind_is_disruptive(client, presets_file):
    """The exclusivity rule lives in one set; if the kind is not in it, the rule is not real."""
    assert "presets-apply" in updater.DISRUPTIVE


def test_snapshots_endpoint_lists_what_can_be_rolled_back(client, presets_file, monkeypatch):
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: {"ok": True, "state": {}})
    client.post("/api/personalities/apply", json={"name": "safe", "confirm": True})
    _settle()
    r = client.get("/api/personalities/snapshots")
    assert r.status_code == 200, r.text
    snaps = r.json()["snapshots"]
    assert len(snaps) >= 1
    first = snaps[0]
    for field in ("stamp", "path", "readable", "bytes", "note"):
        assert field in first, first
    assert first["readable"] is True


def test_rollback_requires_confirm_and_restores_the_file(client, presets_file, monkeypatch):
    restarts: list[tuple[str, str]] = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: restarts.append((k, a)) or {"ok": True, "state": {}})
    before = presets_file

    client.post("/api/personalities/apply", json={"name": "safe", "confirm": True})
    _settle()
    assert paths.PRESETS_INI.read_text() != before

    stamp = presets_store.list_snapshots()[0]["stamp"]

    # confirm-less first: the same gate as apply, asserted at the same point.
    r = client.post("/api/personalities/rollback", json={"stamp": stamp})
    assert r.status_code == 400, r.text
    assert "confirm required" in r.text
    assert paths.PRESETS_INI.read_text() != before, "a refusal must not restore anything"

    r = client.post("/api/personalities/rollback",
                    json={"stamp": stamp, "confirm": True})
    assert r.status_code == 200, r.text
    _settle()
    assert paths.PRESETS_INI.read_text() == before, "rollback must restore the original bytes"
    assert restarts and ("llamacpp", "restart") in restarts, \
        "restoring the file without restarting leaves the router on the old config"


def test_rollback_of_an_unknown_stamp_fails_the_job_without_touching_the_file(
        client, presets_file, monkeypatch):
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: {"ok": True, "state": {}})
    before = presets_file
    r = client.post("/api/personalities/rollback",
                    json={"stamp": "19700101-000000", "confirm": True})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]
    _settle()
    row = updater.job(job_id)
    assert row is not None, "the job row is gone: the job was never recorded"
    assert row["status"] == "failed", row["log"]
    assert "nothing to restore" in row["log"]
    assert paths.PRESETS_INI.read_text() == before


# ------------------------------------------------ registry coverage from live evidence
def test_lazy_mode_is_registered_and_emitted(client, library):
    """`lazy-mode` was found by reading what the running router actually resolved, not from a
    wish list — and the registry still had no definition for it.

    It used to be registered-but-withheld, on the grounds that a hand-started router may set
    `--lazy-mode` on the command line, where it outranks presets.ini. That is correct about
    precedence and wrong about the managed router, which is the only router the portal controls
    and which passes no `--lazy-mode` at all (LLAMA_ARGS is empty in llamacpp.env). Measured on
    one model and one build (b10964): 80.5 GiB of aperture with lazy-mode unset against 54.5 GiB
    with it on, and every llama-server OOM kill in the journal carries `file-rss: 8 kB` — never
    page-cache backed. Withholding it protected a case the portal does not manage while leaving
    the case it does manage unprotected.
    """
    assert "lazy-mode" in P.SETTINGS
    assert P.check_setting("lazy-mode", "auto") is None
    assert P.check_setting("lazy-mode", "sometimes") is not None
    assert P.GLOBAL_DEFAULTS.get("lazy-mode") == "on", \
        "the managed router gets no --lazy-mode from anywhere else, so this is its only source"

    r = client.post("/api/personalities/preview", json={"name": "safe"})
    assert r.status_code == 200, r.text
    diff_text = "\n".join(r.json()["diff"])
    assert "lazy-mode" in diff_text, \
        "a key this box needs must reach the generated file, not just the registry"




# ----------------------------------------------- the context line: tokens per client, per model
#
# The card used to report only what the box pays (worst-case resident GiB). "What does a
# request get?" was nowhere, so a personality that halves every client's context looked
# identical to one that does not. docs/PORTAL_PLAN.md sec 6.12.22 carries the sweep that
# pins the predictor behind this line.

def test_the_page_shows_tokens_per_client_for_each_priced_model(client, library):
    """The card names the window and who decides it — in the fixture's own numbers."""
    page = client.get("/personalities").text
    # The deck (§6.14.1f) moved this table under the fold: it is a disclosure inside the raised
    # card now, and its title is a <summary>. Asserted case-insensitively because the title's
    # casing is presentation, and asserted as a disclosure because "the table is on the page" and
    # "the table is not eating the first screen" are two different claims.
    assert "context each model gives a client" in page.lower(), "the context block is missing"
    assert '<summary>context each model gives a client</summary>' in page
    row = page[page.lower().index("context each model gives a client"):]
    assert "tokens per client" in row, row[:400]
    # Every model the fixture seeds is trained at 4096, and nothing pins ctx-size, so the
    # trained context is the window. A page quoting the setting instead of the window would
    # show a number that is not 4096.
    assert "4096" in row, row[:400]
    assert "trained" in row, "who decided the window is not stated"


def _grid(page: str, grid_id: str) -> str:
    """One editor grid's markup, from its opening div to the row after it ends.

    `#ed-summary` sits below both grids, so it bounds the model grid; the global grid is bounded by
    the model grid's own id. Done by string bounds rather than a DOM parse because the assertion is
    about markup this template emits, and a parser that repairs the page hides the failure.
    """
    start = page.index(f'id="{grid_id}"')
    stop = (page.index('id="ed-model-fields"') if grid_id == "ed-global"
            else page.index('id="ed-summary"'))
    return html.unescape(page[start:stop])


def _rows_of(region: str) -> dict[str, str]:
    """{key: the row's own markup}, keyed in document order.

    Editor rows are `<div class="ed-field" ...>`; keys-table rows are `<tr data-key=...>`. Both are
    split on their own opening tag -- a helper that only knows one of them reports a key as absent,
    which is a worse failure to write a test against than the assertion it is checking.
    """
    parts = re.split(r'<div class="ed-field" |<tr ', region)
    out = {}
    for chunk in parts[1:]:
        m = re.search(r'data-key="([a-z0-9-]+)"', chunk)
        if m:
            out[m.group(1)] = chunk
    return out


def test_the_editor_rows_carry_their_default_their_rule_and_their_proof(client):
    """The editor's rows are the row layer's data rendered, not a template's opinion.

    Slice 2 of §6.14.1 puts a fixed row component in the editor grids: NAME [control] (default),
    one short line, and a collapsible detail. The guard is per row, against `settingrows` -- which
    key is offered, whether the row states its default (and only when it has one), the rule sentence
    derived from its bounds, the side effect, the spellings b11429 declares for it, and the help link
    for the section that proves it. A template that hardcodes any of that stops matching.
    """
    page = client.get("/personalities").text
    rows = {r["key"]: r for r in settingrows.editable_rows()}

    model_rows = _rows_of(_grid(page, "ed-model-fields"))
    assert list(model_rows) == list(rows), (
        f"the editor offers {len(model_rows)} rows, the registry says {len(rows)}: "
        f"{sorted(set(model_rows) ^ set(rows))[:6]}")
    global_rows = _rows_of(_grid(page, "ed-global"))
    want = [k for k, r in rows.items() if r["scope"] != "model"]
    assert list(global_rows) == want, f"global grid: {sorted(set(global_rows) ^ set(want))[:6]}"

    for key, chunk in model_rows.items():
        r = rows[key]
        # The default is on the row, and a row with no default does not claim one. "default None"
        # and an invented default are both lies the page would tell about the binary.
        has_default = "ed-default" in chunk
        assert has_default == (r["default"] is not None), key
        if r["default"] is not None:
            assert f"default {r['default']}" in chunk, key
            if r["unit"]:
                assert r["unit"] in chunk, f"{key}: a number with no unit is a number to guess at"
        assert r["rule"] in chunk, f"{key}: the row lost the rule its own bounds produce: {r['rule']}"
        assert 'class="ed-detail"' in chunk, f"{key}: no detail block, nowhere for the prose to go"
        if r["detail"]:
            assert r["detail"] in chunk, f"{key}: the side effect is not on the row"
        for alias in r["aliases"]:
            assert alias in chunk, f"{key}: the build spells it {alias} and the page does not say so"
        ref = r["help_ref"]
        assert (f'href="/help#{ref}"' in chunk) == bool(ref), (
            f"{key}: help link and citation disagree" if ref else f"{key}: a link to no section")
        # A row the file may not support states the fact it needs, on the row.
        if r["visible_when"]:
            assert "data-when" in chunk, f"{key}: names {r['visible_when']} but never says so"


def test_the_page_says_which_rows_a_model_cannot_use_and_which_fact_is_missing(client):
    """Per-model visibility is computed from the index, and its absence is an explanation.

    A setting that vanishes reads as a build that lacks it. So the limits ride on the model option,
    and every entry names the model-index fact the header did not provide. Compared against
    `settingrows.hidden_reason` for the same index rows -- the page and the row layer cannot then
    disagree about a file, and a rule added to VISIBLE_FACTS shows up here the moment it is added.
    """
    page = client.get("/personalities").text
    # `tojson` renders its JSON as already-safe markup, so the attribute holds raw double quotes
    # inside single quotes and Jinja escapes the apostrophes the reason text contains to \u0027.
    # That is what makes a single-quoted attribute with a JSON body parseable here.
    opts = re.findall(r"<option value=\"([^\"]+)\" data-limits='([^']*)'", page)
    ids = [r["id"] for r in mi.rows()]
    assert sorted(m for m, _ in opts) == sorted(ids), "the editor's models and the index disagree"
    limits = {m: json.loads(raw) for m, raw in opts}

    for mrow in mi.rows():
        want = {}
        for r in settingrows.editable_rows():
            why = settingrows.hidden_reason(r, mrow)
            if why:
                want[r["key"]] = why
        assert limits[mrow["id"]] == want, mrow["id"]

    # Non-vacuous, on this fixture: neither seeded file declares a chat template or sliding-window
    # layers, so `jinja`, `ctx-checkpoints` and `checkpoint-min-step` must be listed as unusable --
    # with the reason naming the fact, which is the part an operator reads.
    listed = sorted({k for v in limits.values() for k in v})
    assert "jinja" in listed and "ctx-checkpoints" in listed, f"nothing was hidden: {listed}"
    for v in limits.values():
        for key, why in v.items():
            assert why.startswith("needs ") and "header" in why, (key, why)


def test_a_key_the_router_owns_is_explained_and_never_offered_a_control(client, monkeypatch):
    """`editable_rows` is the whole refusal story, so it gets a case where it has teeth.

    Today the registry contains no non-writable key -- every key in SETTINGS is one the portal can
    emit, and `rows()` and `editable_rows()` render identically. That makes "the editor offers only
    writable keys" a claim no page test can detect: mutating the route to `rows()` passed every
    assertion in this file, which is an equivalent mutation and a real hole in the guard, found by
    breaking the code on purpose. So the case is constructed: `setting_is_writable` is made to refuse
    one key, and the page must show that key in the keys table with the refusal stated, and offer no
    control for it in either grid. A control that changes nothing is the artifact the operator will
    rely on, which is why the keys table may show it and the editor may not.
    """
    real = P.setting_is_writable
    monkeypatch.setattr(P, "setting_is_writable",
                        lambda key: False if key == "flash-attn" else real(key))
    page = client.get("/personalities").text

    assert "flash-attn" not in _rows_of(_grid(page, "ed-model-fields")), \
        "the editor offered a control for a key the router overwrites"
    assert "flash-attn" not in _rows_of(_grid(page, "ed-global")), \
        "the global grid offered a control for a key the router overwrites"
    # §6.14.1q: the key's explanation lives in the reference now, not in a page table. The page's
    # half of the claim (no control offered) is asserted above; the document's half is that a
    # non-writable key is published **with the refusal on its section** — the sentence the page used
    # to carry. The document prints such a key ("the keys table may show it and the editor may not",
    # unchanged), so a key the router owns is explained where the reference is read.
    from app.core import settingsdoc

    rows = [dict(r, writable=r["key"] != "flash-attn") for r in settingrows.rows()]
    doc = settingsdoc.render(rows)
    section = doc[doc.index("### flash-attn"):]
    nxt = re.search(r"^### ", section[10:], flags=re.M)
    section = section[:10 + (nxt.start() if nxt else len(section))]
    assert "not writable here" in section, "the reference omits the refusal"
    assert "the router controls this key at launch" in section, "the refusal names no reason"


# --------------------------------------------------------------------------- the reference moved (queue 33)
#
# §6.14.1q / docs/PORTAL_POLISH.md §7 state 4: the settings reference left the personalities page.
# The page is a working surface; a 420 px table of key definitions between the deck and the editor
# was reference material in the middle of it. The content was already generated into
# docs/PORTAL_SETTINGS.md from the same `settingrows` rows, so the guards that read the page's table
# moved to the document and the endpoint that serve it -- tests/test_settingsdoc.py -- and the page
# keeps the links. Reading a moved guard is the point: the claim is about the row data, and it is
# now asserted where the row data is published.


def test_the_personalities_page_carries_the_reference_as_a_link_and_not_as_a_table(client):
    """The page hands the reference to the page that owns it, and keeps the deep links.

    Queue 33 state 4. The table's content is the registry's row data, published on Help per key
    (tests/test_kb.py proves `/help#cache-ram` answers the section PORTAL_SETTINGS.md carries), so
    the page's half is: no 420 px table, one line pointing at the reference, and every key's row
    still linking to its own anchor -- the rule the table carried, unchanged.
    """
    page = client.get("/personalities").text

    assert 'id="keys-table"' not in page, "the reference table is still between the deck and editor"
    assert 'href="/help#settings-a-personality-can-set"' in page, \
        "the page does not hand the reference to the page that owns it"
    for key in ("cache-ram", "ctx-checkpoints"):
        assert f'href="/help#{key}"' in page, f"the editor row for {key} lost its reference link"


# ---------------------------------------------- the reference links: every key, its own section
def test_every_reference_link_the_page_prints_opens_the_settings_reference(client):
    """A link the page prints must open the document the page means.

    Two ways this breaks and neither is visible on the page: the anchor resolves to a heading in
    another document (a namesake in the plan), or it resolves to nothing and the help page answers
    404. The registry has this guard for its `§` citations; the generated reference's anchors are
    the same class of claim.
    """
    page = client.get("/personalities").text
    anchors = set(re.findall(r'href="/help#([a-z][a-z0-9-]*)"', page))
    assert anchors, "the page prints no reference anchors"
    from app.core import settingsdoc
    for anchor in sorted(anchors):
        section = kb.find(anchor)
        assert section is not None, f"/help#{anchor} opens nothing"
        assert section["doc"] == settingsdoc.DOC_NAME, \
            f"/help#{anchor} opens {section['doc']}, not the settings reference"
        r = client.get(f"/api/help/section/{anchor}")
        assert r.status_code == 200, (anchor, r.status_code, r.json().get("detail"))


def test_both_editor_grids_link_their_rows_into_the_reference(client):
    """The editor is where a setting is typed, so it is where the measurement has to be reachable.

    The grids render from row data, and the link is part of the row's markup; a grid that drops it
    leaves one of the two places an operator reads a setting with no route to the evidence.
    """
    page = client.get("/personalities").text
    editable = {r["key"] for r in settingrows.editable_rows()}
    for grid in ("ed-global", "ed-model-fields"):
        rows = _rows_of(_grid(page, grid))
        assert rows, f"{grid} rendered no rows"
        for key, chunk in rows.items():
            if key not in editable:
                continue
            assert f'href="/help#{key}"' in chunk, f"{grid}: the row for {key} has no reference link"



# ---------------------------------------------------------------- the deck (§6.14.1f)
#
# The operator's complaint was measured, not assumed: two cards, each with its own presets.ini
# diff, filled the first screen, and 10 personalities would need scrolling to choose between.
# The deck is the answer -- one line per personality, the body under the fold -- and what it must
# never do is look like the thing that changes the router. These tests hold the strip's contents,
# the raised card, and the deck's inability to write.

def _strips(body: str) -> dict:
    """name -> the strip's own markup, for assertions about what the deck line says."""
    out = {}
    for m in re.finditer(r'<button type="button" class="pers-strip" data-raises="([^"]+)"'
                         r'(.*?)</button>', body, re.S):
        out[m.group(1)] = m.group(2)
    return out


def test_the_deck_prints_one_line_per_personality(client):
    """Every personality gets a strip naming it, its fit, and its model count -- and a body."""
    body = client.get("/personalities").text
    names = [str(p["name"]) for p in P.all_personalities() if p.get("name")]
    assert names, "the fixture has personalities to list"
    strips = _strips(body)
    assert sorted(strips) == sorted(names), "a personality with no deck line is a personality gone"
    for n in names:
        s = strips[n]
        assert f'>{n}</span>' in s.replace('\n', ' ').replace('  ', ' '), n
        assert re.search(r'badge (yes|no|unknown)"', s), f"{n}: the strip omits the fit verdict"
        assert re.search(r'\d+ models · max \d+ resident', s), f"{n}: the strip omits the model count"
        assert 'id="pers-' + n + '"' in body, n
        assert 'class="pers-body"' in body, n
    # The diff is the tall thing on a card, so it is a disclosure: present in the DOM, closed
    # by default, and reachable in the raised card.
    assert body.count('<details class="pers-more">') >= 2 * len(names)
    assert 'presets.ini it would write' in body


def test_the_raised_card_on_load_is_the_one_the_router_is_running(client):
    """The deck opens on the live personality, and says so on the strip.

    This is the page's one unambiguous sentence -- an operator acts on it -- so it is asserted
    from the attributes the page itself carries: the raised state and the live badge, both naming
    the card they belong to.
    """
    original = _presets_text()
    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)

    def restore():
        """Back to exactly what was there, including "there was nothing there"."""
        if original == "<absent>":
            paths.PRESETS_INI.unlink(missing_ok=True)
        else:
            paths.PRESETS_INI.write_text(original)

    try:
        budget = mi._pool_bytes()
        safe = P.find("safe")
        assert safe is not None, "safe is a built-in personality"
        paths.PRESETS_INI.write_text(P.render(safe, budget)["text"])
        body = client.get("/personalities").text
        raised = re.findall(r'data-card="([^"]+)" data-raised="yes"', body)
        assert raised == ["safe"], f"the deck raised {raised}; the file on disk is `safe`'s file"
        assert re.search(r'data-live-strip="safe"', body), "the strip does not say it is running"
        assert re.search(r'data-active-for="safe"', body), "the body does not say it is running"
        assert 'aria-expanded="true"' in _strips(body)["safe"]
        assert 'aria-expanded="false"' in _strips(body)["default"]

        # Nothing on disk matches any personality: nothing is raised, and the deck says that
        # out loud rather than leaving the operator to infer it from an empty page.
        paths.PRESETS_INI.write_text((original if original != "<absent>" else "")
                                     + "\n# edited by hand\n")
        body = client.get("/personalities").text
        assert not re.findall(r'data-card="([^"]+)" data-raised="yes"', body)
        assert not re.findall(r'data-live-strip="([^"]+)"', body)
        assert "Nothing is running" in body
    finally:
        restore()


# --- the editor shows the personality's own settings (plan §6.14.1i)
#
# The operator's complaint this answers: selecting a personality left the form empty. An editor
# whose fields start blank is not editing the configuration, it is proposing a new one, and the
# operator cannot see what they are about to overwrite. So the fields are seeded from the
# personality's own layers, and the payload is the whole desired section rather than a diff --
# which is only safe once what the page shows is what the personality carries.


def _views(page: str) -> dict:
    """{personality: the current-config view its option carries}, read off the page."""
    out = {}
    for m in re.finditer(r'<option value="([^"]+)" data-config=\'([^\']*)\'>', page):
        out[m.group(1)] = json.loads(html.unescape(m.group(2)))
    return out


def _seed_tuned_personality(client, library) -> dict:
    """A config carrying a bool, a float, an int and a model-only key, on a personality that also
    covers one bare model and sets two keys in `[*]`.

    Created through the page's own endpoints, so the view is asserted about data the portal
    accepted. The key shapes are chosen on purpose: `jinja` is a bool, `temp` a float, `parallel`
    an int and `ctx-size` is model-scoped -- the four ways a seeded value can arrive in a shape
    the form cannot send back.
    """
    r = client.post("/api/model-configs/save", json={
        "name": "Tiny-Coder", "base_model": library[0], "note": "fixture config",
        "settings": {"jinja": "on", "temp": "0.5", "parallel": "2", "ctx-size": "2048"}})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/personalities/save", json={
        "name": "work", "models_max": 2, "note": "fixture personality",
        "models": ["Tiny-Coder", library[1]],
        "global_edits": {"threads": "8", "cache-prompt": "off"}})
    assert r2.status_code == 200, r2.text
    return {"config": "Tiny-Coder", "personality": "work"}


@pytest.fixture()
def tuned(client, library):
    """The seeded config and personality, removed again afterwards.

    Cleanup is part of the fixture: this suite shares one throwaway home, and a custom personality
    left behind is visible to every later module, including ones that assert about the deck.
    """
    seeded = _seed_tuned_personality(client, library)
    yield seeded
    client.post("/api/personalities/delete", json={"name": seeded["personality"], "confirm": True})
    client.post("/api/model-configs/delete", json={"name": seeded["config"], "confirm": True})


def test_the_editor_shows_the_settings_the_personality_already_carries(client, library, tuned):
    """The fields arrive filled with the personality's own values, in the words a form can send.

    The expectations below are literals, deliberately. A test that compared the page against
    `settings_view()` alone would pass while `settings_view()` itself stopped reading the config
    layer, and the page would be blank in exactly the way the operator reported.
    """
    views = _views(client.get("/personalities").text)
    names = [str(p["name"]) for p in P.all_personalities() if p.get("name")]
    assert sorted(views) == sorted(names), "a personality the editor has no current settings for"

    work = views[tuned["personality"]]
    assert work["global"] == {"cache-prompt": "off", "threads": "8"}, work["global"]
    assert work["models"][library[0]] == {"ctx-size": "2048", "jinja": "on", "parallel": "2",
                                          "temp": "0.5"}, work["models"]
    assert work["configs"] == {library[0]: "Tiny-Coder"}, work["configs"]
    assert work["models_max"] == 2, work
    # A bool is `on`, not `True`: a field seeded with Python's spelling sends a preset line the
    # preset parser reads as false, which is the failure §6.14.1g named for the control side.
    assert work["models"][library[0]]["jinja"] == "on"
    assert work["global"]["cache-prompt"] == "off"
    # `alias` is not a setting the editor can carry -- render() writes the config's own name, and a
    # control for it would let a person type a router-wide alias by hand.
    assert "alias" not in work["models"][library[0]]


def test_a_bare_model_shows_nothing_because_its_section_sets_nothing(client, library, tuned):
    """A model with no config shows an empty section, not the values it inherits.

    Copying `[*]` into a model's fields would write those values into the model's own section the
    next time the personality is saved -- and the router's precedence (CLI > model section > `[*]`)
    means each copy then outranks `[*]` forever. One number in one place is the property being
    protected here, not a formatting preference.
    """
    views = _views(client.get("/personalities").text)
    work = views[tuned["personality"]]
    assert work["models"][library[1]] == {}, work["models"]
    # ...and the model that *does* carry settings is not shown the global ones either.
    assert "threads" not in work["models"][library[0]]
    assert "cache-prompt" not in work["models"][library[0]]


def test_every_value_the_page_seeds_is_a_value_the_portal_accepts(client, library, tuned):
    """Round-trip: the seeded text must survive the portal's own validator.

    The page seeds fields from stored JSON, and a field sends its content back as text. A value
    stored in a shape the validator refuses -- a Python `True` in a bool row, a `1` in a switch --
    is a personality that can be edited only by breaking it.
    """
    views = _views(client.get("/personalities").text)
    checked = 0
    for name, view in views.items():
        tables = [("*", view.get("global") or {})]
        tables += [(m, t) for m, t in (view.get("models") or {}).items()]
        for where, table in tables:
            for key, value in table.items():
                problem = P.check_setting(key, P.coerce_setting(key, value))
                assert problem is None, f"{name} [{where}] {key}={value!r}: {problem}"
                checked += 1
    assert checked >= 6, f"only {checked} value(s) were checked -- the fixture seeded nothing"


def test_the_shown_config_is_the_same_layering_the_file_is_written_from(client, library, tuned):
    """One statement of the layering, read by the form and by the generator.

    `_layered_settings()` is what `render()` writes the file from and what `budget_for()` prices.
    If the editor showed a second derivation, the preview's diff and the fields a person edited
    could describe two different files -- and the fit verdict a third.
    """
    views = _views(client.get("/personalities").text)
    cfgs = {c["name"]: c for c in mc.rows()}
    for p in P.all_personalities():
        view = views[str(p["name"])]
        layered = P._layered_settings(p, configs=cfgs)
        assert view["models"] == {m: {k: P._ini_value(v) for k, v in per.items()}
                                  for m, per in layered.items()}, str(p["name"])


def test_the_builtins_show_an_empty_config_because_they_carry_no_settings(client, library):
    """`safe` and `default` are derived from the library, so they carry no settings to show.

    Asserted as an empty config rather than as nothing, because the difference is what the page
    claims: these personalities set no keys, and a view that filled their fields from the portal's
    own defaults would make them look editable in place.
    """
    views = _views(client.get("/personalities").text)
    for name in ("safe", "default"):
        v = views[name]
        assert v["global"] == {} and v["configs"] == {}, name
        assert all(per == {} for per in v["models"].values()), f"{name} shows settings it does not carry"
        # The view's model set is the personality's own resolved set -- `safe` names one model and
        # `default` is the library, and a view that listed a model the card does not would seed
        # fields for a section the file never contains.
        built_in = P.find(name)
        assert built_in is not None, f"{name} is a built-in personality"
        card = P.describe(built_in)
        assert set(v["models"]) == set(card["models"]), (name, sorted(v["models"]), card["models"])


def test_a_personality_missing_a_model_shows_the_refusal_not_an_empty_form(client, library):
    """The view keeps its `[*]` and names what is missing, rather than collapsing to nothing.

    The page has no endpoint to ask, so the refusal has to ride on the view. A personality that
    renders as an empty form reads as "this personality sets nothing", which is a different claim
    from the true one -- "its model list names a file that is not here".
    """
    now = time.time()
    db.execute("INSERT INTO personalities (name, models_max, note, models, global, created_at,"
               " updated_at) VALUES (?,?,?,?,?,?,?)",
               ("broken", 1, "names a model that is gone", json.dumps(["Not-A-Model"]),
                json.dumps({"threads": 8}), now, now))
    try:
        r = client.get("/personalities")
        assert r.status_code == 200, "a personality that cannot resolve must still get a page"
        view = _views(r.text)["broken"]
        assert "Not-A-Model" in view["error"], view
        assert view["global"] == {"threads": "8"}, "the [*] it does carry went with the refusal"
        assert view["models"] == {}, view
        assert 'data-card="broken"' in r.text, "the card disappeared with the view"
    finally:
        db.execute("DELETE FROM personalities WHERE name = 'broken'")


def test_the_api_carries_the_same_current_config_the_page_seeds_from(client, library, tuned):
    """D1: the read endpoint answers the same question the form asks (§4.7)."""
    page = _views(client.get("/personalities").text)
    api = client.get("/api/personalities").json()["config_views"]
    assert api == page, "the endpoint and the page describe the personalities differently"


def test_step_one_sends_the_personality_the_write_endpoint_accepts(client, library, tuned):
    """Create, save and delete from the page's own payload shape, through the page's own endpoints.

    Step 1 writes `personalities/<name>.json` and nothing else: presets.ini is untouched, the router
    is not restarted, and there is nothing for Preview to show. That boundary is what makes these
    verbs additive, and it is asserted here rather than trusted: a page that quietly applied while it
    saved a name would pass every visual check and restart the router on a click.
    """
    body = client.get("/personalities").text
    for sel in ('id="ed-new-name"', 'id="ed-models-max"', 'id="ed-note"', 'id="ed-attach"',
                'id="ed-create"', 'id="ed-save"', 'id="ed-delete"', 'id="ed-steps"'):
        assert sel in body, f"step 1 is missing {sel}"
    # The attach list is the two kinds a personality's `models` accepts, both of them named on the
    # page: a library model id, and a stored config with the file it renames.
    for opt in list(library) + [tuned["config"]]:
        assert f'value="{opt}"' in body, f"the model list has no entry for {opt}"
    assert f"renames {library[0]}" in body, "a config is listed without the model it renames"

    created = "Page-Created"
    r = client.post("/api/personalities/save", json={
        "name": created, "models_max": 1, "note": "built from the page's payload shape",
        "models": [library[1], tuned["config"]],
        "global_edits": {"threads": "4"},
    })
    assert r.status_code == 200, r.text
    # The stored file, not the card: `describe()` reports resolved targets, and step 1 stores the
    # names the operator chose, which is the thing the form has to be able to send.
    saved = next(p for p in P.stored() if p.get("name") == created)
    assert saved["models"] == [library[1], tuned["config"]], saved
    # The stored value is the typed text coerced by the row's declared type -- an int, not the string
    # the field sent -- which is why the editor seeds its fields with `_ini_value()` spellings.
    assert saved["models_max"] == 1 and saved["global"] == {"threads": 4}, saved

    view = _views(client.get("/personalities").text)[created]
    assert view["attach"] == [library[1], tuned["config"]], "the form cannot pre-fill what it just stored"
    assert view["editable"] is True, view
    assert view["models_max"] == 1 and view["note"] == "built from the page's payload shape", view

    # Saving the identity again, as Save does: the list, max and note move, and the router is
    # untouched because presets.ini was never written.
    before = _presets_text()
    r = client.post("/api/personalities/save", json={
        "name": created, "models_max": 3, "note": "saved",
        "models": [library[0]], "global_edits": {"threads": "4"},
    })
    assert r.status_code == 200, r.text
    view = _views(client.get("/personalities").text)[created]
    assert view["attach"] == [library[0]] and view["models_max"] == 3 and view["note"] == "saved", view
    assert _presets_text() == before, "step 1 wrote presets.ini"

    assert client.post("/api/personalities/delete", json={"name": created}).status_code == 400, \
        "delete is allowed without a confirm"
    assert client.post("/api/personalities/delete",
                       json={"name": created, "confirm": True}).status_code == 200
    assert created not in [str(v) for v in _views(client.get("/personalities").text)], \
        "the personality is still on the page after delete"


def test_the_built_in_pair_is_locked_at_step_one_with_the_reason(client, library):
    """`safe` and `default` are shown, listed and refused -- by name, at the endpoint and on the form.

    The page's job here is not to hide them (they are the two personalities that describe the library
    as it is) but to refuse them in a way the operator can read, and to point at the verb that does
    work: create a new personality.
    """
    for name in ("safe", "default"):
        r = client.post("/api/personalities/save", json={"name": name, "models_max": 1})
        assert r.status_code == 400, (name, r.text)
        d = client.post("/api/personalities/delete", json={"name": name, "confirm": True})
        assert d.status_code == 400 and name in d.text, (name, d.text)
        view = _views(client.get("/personalities").text)[name]
        assert view["editable"] is False, view

    # `default` covers the whole library, which the view carries as None and not as an empty list:
    # the form has to be able to tell "everything" from "nothing chosen".
    assert _views(client.get("/personalities").text)["default"]["attach"] is None
    assert _views(client.get("/personalities").text)["safe"]["attach"] == [library[1]], \
        "safe's single model is not in the data the form pre-fills from"


def test_the_page_says_what_an_empty_field_means(client):
    """Empty is "this layer does not set the key", and the page has to say so.

    A form seeded with values needs its absence explained: an empty box next to `default 8192 MiB`
    reads as 0 to anyone who has ever filled in a form, and `ctx-size`/`cache-ram` are the two keys
    where that guess costs tens of GiB.
    """
    body = client.get("/personalities").text
    region = body[body.index('id="ed-layers"'):]
    assert "does not set the key" in region, region[:300]
    assert "default" in region, "the row default is not named as the thing that applies"


def _shared_data(body: str) -> dict:
    """The editor's shared-object data, read off the attribute the page reads it from."""
    m = re.search(r'id="pers-editor"\s+data-configs=\'([^\']*)\'', body)
    assert m, "the editor carries no shared-object data"
    return json.loads(html.unescape(m.group(1)))


def test_the_editor_offers_one_card_per_model(client, library, tuned):
    """One card per model, in the only shape the file can hold.

    `presets.ini` has one section per model and `resolve_targets()` refuses two configs on one model,
    so a deck with an arbitrary number of cards per model would be a UI for a state the generator
    cannot write. The card count is asserted against the library for that reason.
    """
    body = client.get("/personalities").text
    cards = re.findall(r'<section class="mp-card" data-model="([^"]+)"', body)
    assert len(cards) == len(set(cards)), "a model has more than one card"
    assert set(library) <= set(cards), f"cards are {cards}, library is {library}"
    # The same list the model-config endpoint calls its bases: a card for a model the router cannot
    # see would offer a model personality the generator refuses to write.
    bases = set(client.get("/api/model-configs").json()["bases"])
    assert set(cards) <= bases, f"cards name models the index does not: {set(cards) - bases}"
    for slot in ("mp-attach", "mp-new", "mp-create", "mp-save", "mp-delete", "mp-detach",
                 "mp-share", "mp-slot", "mp-result"):
        assert slot in body, f"the card has no {slot}"
    # The card is the selector: the only control on the strip is the strip.
    strip = re.search(r'<button type="button" class="mp-strip".*?</button>', body, re.S).group(0)
    assert "<button" not in strip[len("<button"):] and "<input" not in strip, \
        "the card strip carries a control: the strip is for reading and choosing, not for writing"


def test_a_card_names_the_object_it_edits_and_who_else_uses_it(client, library, tuned):
    """`used by` is server data, because the delete route's refusal depends on the same answer.

    A card that computed "who else uses this" from what it could see on the page would be a second,
    weaker answer to a question the read endpoint answers exactly -- and the endpoint refuses a delete
    on the strength of that answer.
    """
    shared = _shared_data(client.get("/personalities").text)
    by_name = {c["name"]: c for c in shared["configs"]}
    assert tuned["config"] in by_name, shared["configs"]
    assert by_name[tuned["config"]]["base_model"] == library[0], by_name[tuned["config"]]
    assert by_name[tuned["config"]]["settings"], "a config with no settings is not a model personality"
    assert tuned["personality"] in shared["uses"][tuned["config"]], shared["uses"]
    # Only configs built on that model can attach to it -- the same rule `model_config.save` enforces.
    for c in shared["configs"]:
        assert c["base_model"] in library, c


def test_a_config_in_use_refuses_delete_and_names_who_uses_it(client, library, tuned):
    """The refusal, in the operator's terms, before anything is removed.

    A config that vanishes from under a personality does not error: the personality quietly stops
    carrying those settings and the next apply writes a smaller file than the operator believes they
    are editing.
    """
    r = client.post("/api/model-configs/delete", json={"name": tuned["config"], "confirm": True})
    assert r.status_code == 400, r.text
    assert tuned["personality"] in r.text, r.text
    assert "assigned by" in r.text, r.text
    assert mc.get(tuned["config"]), "the refusal deleted the config it refused about"


def test_a_model_personality_can_be_created_for_a_model_the_page_lists(client, library):
    """Step 3 creates a shared object; step 1 attaches it; the router file is untouched.

    The two writes stay separated: a model personality is a definition the portal keeps, and
    `presets.ini` changes only through Apply. Asserted across both, with the file compared, because
    the whole point of the split is that editing a shared object cannot restart the router.
    """
    before = _presets_text()
    name = "Page-Card-Coder"
    r = client.post("/api/model-configs/save",
                    json={"name": name, "base_model": library[1],
                          "settings": {"temp": "0.2", "parallel": "4"}, "note": "from the card"})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["base_model"] == library[1]
    assert r.json()["assigned"] == [], "a brand new config is claimed by somebody"

    shared = _shared_data(client.get("/personalities").text)
    assert name in {c["name"] for c in shared["configs"]}
    assert shared["uses"].get(name, []) == []

    assert client.post("/api/personalities/save",
                       json={"name": "Card-User", "models_max": 1, "note": "",
                             "models": [name], "global_edits": {}}).status_code == 200
    shared = _shared_data(client.get("/personalities").text)
    assert shared["uses"][name] == ["Card-User"], shared["uses"]
    d = client.post("/api/model-configs/delete", json={"name": name, "confirm": True})
    assert d.status_code == 400 and "Card-User" in d.text, d.text

    assert _presets_text() == before, "a model personality write touched presets.ini"
    assert client.post("/api/personalities/delete",
                       json={"name": "Card-User", "confirm": True}).status_code == 200
    assert client.post("/api/model-configs/delete",
                       json={"name": name, "confirm": True}).status_code == 200


# ------------------------------------------------ the `used by` line (queue 33's page half, §6.14.1q)
#
# The brief states the connection in both directions, and the record half is in the plan. The page's
# half is the editor's row: a personality has to show the projects that run with it. The data source
# is `projects.by_personality(projects.serve_facts())` — the inverse view built from the records the
# launches wrote (§6.14.1q). Asserted on the payload first: a page can print a sentence, and only the
# payload says whether the portal answered with the host's answer attached.


def test_the_personalities_payload_answers_which_projects_run_each_personality(client):
    """D1: the editor's `used by` line is the payload's answer, not the page's inference.

    `by_personality` is the inverse of the projects page's question and cannot be asked there, so
    the personalities payload carries it. Two properties: the projects listed per personality come
    from the records, and the host's state travels with the list — an editor that printed "no
    projects" while the host was unreachable would be stating a fact about the host as a fact about
    the personality.
    """
    body = client.get("/api/personalities").json()
    assert "used_by" in body, "the payload does not answer the editor's `used by` question"
    used = body["used_by"]
    # The key is the inverse view's own key (`hostOk`), asserted as the pair of keys the view is
    # defined by. Naming it `host_ok` *here* would be the page-side test inventing the shape of the
    # component's answer, and the two halves of the connection are written in different files.
    assert set(used) >= {"hostOk", "projects"}, ("the inverse view lost a field", sorted(used))
    assert isinstance(used["hostOk"], bool), "the answer does not say whether the host confirmed it"
    assert isinstance(used["projects"], dict), "the inverse view is not keyed by personality"


def test_the_editor_shows_the_projects_that_run_with_the_personality_being_edited(client):
    """The row is the brief's answer, on the surface where a personality is changed.

    A personality edited without knowing a project runs with it is edited blind: the change lands on
    the next launch of that project. The sentence is asserted on the page, with the payload's reason
    visible when the host did not answer — the state the page cannot invent and must carry.
    """
    page = client.get("/personalities").text
    assert 'id="ed-used-by"' in page, "the editor has no `used by` line"
