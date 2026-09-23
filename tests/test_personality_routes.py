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

from fastapi.testclient import TestClient  # noqa: E402

from app.core import config as cfg  # noqa: E402
from app.core import db, modelindex as mi, paths  # noqa: E402
from app.core import personality as P  # noqa: E402
from app.core import presets_store, systemd, updater  # noqa: E402
from app.main import app  # noqa: E402
from tests.test_modelindex import _write_fake_gguf  # noqa: E402

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
    # SAFE names one specific model, so the library has to contain it or `safe` has no
    # budget to render — legitimately, because a fallback that points at a model that is
    # not on disk is a broken fallback. Naming it here is what lets the budget line be
    # asserted for both cards instead of one.
    names = ((P.SAFE["models"][0], 8, 4000), ("Big-30B", 48, 9000))
    for name, blocks, pad in names:
        d = paths.LLAMA_MODELS / name
        d.mkdir(parents=True, exist_ok=True)
        _write_fake_gguf(d / "m.gguf", {
            "block_count": blocks, "attention.head_count": 4,
            "attention.head_count_kv": 2, "embedding_length": 256,
            "context_length": 4096,
        }, pad=pad)
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
    buttons = [b for b in editor.split("<button")[1:]]
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


