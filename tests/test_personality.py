"""Tests for app/core/personality.py (§4.7 personalities, Phase N4).

These run against a throwaway AI_PORTAL_HOME and a hand-built model_index. Nothing here
touches ~/ai, systemd, or the network.

The interesting cases are the ones where a personality LOOKS fine:
  * models_max=1 over a library of 90 GiB models — fits, but only just, and only because
    one resident is the rule;
  * models_max=2 over the same library — the same models, a number nobody would think to
    check, and it does not fit;
  * an unpriceable model, which must make the worst case UNKNOWN, not "it fits".
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

# Deliberately NOT `os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(...))`, even
# though tests/test_modelindex.py and tests/test_ui_contract.py both do that.
#
# db.DB is `paths.PORTAL_STATE / "portal.db"`, resolved once when paths is first imported,
# so the first test module to run in a pytest process fixes the database path for every
# later module. pytest collects alphabetically, and this file sorts before test_ui_contract
# and test_ui_integration: giving the process a tmp_path here means those suites inherit a
# directory pytest deletes when this module finishes, and they fail rendering pages with a
# TypeError that points at nothing in this file. It took bisecting the suite to find it.
#
# This module also does not need its own home: it never scans a model tree and never
# renders a page. It writes model_index rows, and scripts/check-ui.sh already runs the
# whole suite under one sandboxed home. The env var is set only when this file is run on
# its own, and setdefault means an existing sandbox always wins over this one.
os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-personality-shared"))

from app.core import db  # noqa: E402
from app.core import modelindex as mi  # noqa: E402
from app.core import personality as P  # noqa: E402

# ---------------------------------------------------------------- fixtures

FAKE_META = {
    "block_count": 8,
    "attention.head_count": 4,
    "attention.head_count_kv": 2,
    "embedding_length": 256,
    "context_length": 4096,
}


def _index_row(mid: str, *, est_total: int, est_weights: int | None = None,
               est_ctx: int = 4096, ctx_trained: int = 4096) -> None:
    """Insert one priced row directly.

    personality.py reads modelindex.rows() and never rescans, so seeding the table is
    the honest way to test it: the module under test is being given exactly the input it
    will get from the page, and no filesystem scan is involved in the assertion.
    """
    conn = db._conn()
    # Static SQL text, nine ? placeholders, nine bound values. Written out rather than
    # assembled from a list: the previous version built the column list with an f-string
    # and was correct, but it asked every reader (and every scanner) to re-derive that
    # the joined list was a literal. Values are still parameters, never interpolated.
    sql = ("INSERT OR REPLACE INTO model_index"
           " (id, path, bytes, shard_total, est_weights, est_kv, est_total, est_ctx,"
           "  ctx_trained, scanned_at)"
           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    vals = (mid, f"/nonexistent/{mid}.gguf", est_total, 1, est_weights,
            est_total - (est_weights or est_total), est_total, est_ctx, ctx_trained,
            time.time())
    # The fixture this file hangs on: ten columns, ten placeholders, ten values. Assert
    # the count here so a column added to model_index breaks loudly instead of sqlite
    # raising deep inside an unrelated test.
    assert sql.count("?") == 10 and len(vals) == 10, "fixture drift vs model_index schema"
    # shard_total is not optional in practice even though the column allows NULL: the
    # models page renders `m.shard_total > 1`, and Jinja raises TypeError on None > 1.
    # Leaving it out of a seeded row was enough to break page rendering in a *different*
    # test file, because these rows outlive this module in the shared portal.db.
    assert vals[3] is not None, "shard_total must be an int for the models page to render"
    conn.execute(sql, vals)
    conn.commit()


@pytest.fixture(autouse=True)
def _schema(tmp_path, monkeypatch):
    """An initialized schema in the throwaway home, before any test seeds a row.

    model_index is created by modelindex.init(), not by db.init().

    Deliberately NOT a per-test module reload. An earlier version of this fixture called
    importlib.reload(db) and reload(mi) to re-point paths at a fresh tmp_path, and it
    broke tests in other files: sibling modules keep their own references to the original
    db/modelindex objects, so they went on using a DB path whose directory had been
    removed. Two unrelated suites failed on a TypeError in template rendering, and the
    cause was invisible from inside this file. Isolation here comes from the module-level
    AI_PORTAL_HOME above plus monkeypatching the specific attributes a test cares about.
    """
    db.init()
    mi.init()
    yield


@pytest.fixture
def library(tmp_path, _schema, monkeypatch):
    """A tiny priced library whose PEAK cost is 64, 34 and 5 GiB.

    peak_bytes() is weights + KV + HEADROOM_BYTES, and HEADROOM_BYTES is 4 GiB, so peak
    is est_total + 4 GiB whenever est_ctx matches the ctx being priced (which is the case
    here, and is what `if ctx == row.get("est_ctx")` in peak_bytes decides). The expected
    numbers in the budget tests below are peaks, not file sizes.

    est_weights must be non-zero: peak_bytes() returns None when weights are falsy, so a
    fixture using est_weights=0 silently becomes an unpriced model and every budget
    assertion in this file would be testing the unpriceable path by accident.

    The DELETE is the load-bearing line. The whole suite shares one portal.db, and files
    that sort before this one (test_modelindex, test_llama_smoke) leave rows behind. A
    personality with models=None means "the whole library", so without clearing the table
    these tests were asserting 64/34/5 GiB against whatever three models some earlier
    test happened to leave in the database — they passed alone and failed in the full gate.
    """
    db._conn().execute("DELETE FROM model_index")
    db._conn().commit()
    monkeypatch.setattr(mi, "_pool_bytes", lambda: 100 * 2**30)
    # RAM is a second, independent ceiling from the aperture. Left to read /proc/meminfo, every
    # budget test in this file would depend on how much memory this box happens to have. Pinned
    # well above any worst case here so a test about the aperture stays about the aperture, and the
    # RAM ceiling gets its own tests that lower it on purpose.
    monkeypatch.setattr(mi, "ram_total_bytes", lambda: 200 * 2**30)
    _index_row("huge", est_total=60 * 2**30, est_weights=55 * 2**30)
    _index_row("medium", est_total=30 * 2**30, est_weights=27 * 2**30)
    _index_row("small", est_total=1 * 2**30, est_weights=1 * 2**30)
    return tmp_path


# ---------------------------------------------------------------- model resolution

def test_default_personality_covers_the_whole_library(library):
    per = {"name": "default", "models_max": 1, "models": None}
    assert P.resolve_models(per) == ["huge", "medium", "small"]


def test_a_named_model_with_no_library_entry_is_an_error_not_a_silent_drop():
    """The alternative is a personality that quietly loses a model. The operator would
    see a working-looking box missing the model they were about to depend on."""
    with pytest.raises(P.PersonalityError) as exc:
        P.resolve_models({"name": "typo", "models_max": 1, "models": ["NoSuchModel"]})
    assert "NoSuchModel" in str(exc.value)


# ---------------------------------------------------------------- budget math

def test_worst_case_is_the_heaviest_models_max_combination(library):
    """Hand-computed from peaks: huge 60+4=64, medium 30+4=34, small 1+4=5.
    models_max=2 takes the two heaviest: 64 + 34 = 98 GiB. Not 64+5 (that is not the
    heaviest pair), not 64+34+5 (that is models_max=3). Exact numbers, not a range."""
    b = P.budget_for({"name": "x", "models_max": 2, "models": None})
    assert b.worst_case_bytes == 98 * 2**30
    assert b.heaviest == ["huge", "medium"]


def test_models_max_one_costs_only_the_single_heaviest_model(library):
    b = P.budget_for({"name": "x", "models_max": 1, "models": None})
    assert b.worst_case_bytes == 64 * 2**30
    assert b.headroom_bytes == 36 * 2**30
    assert b.fits is P.FITS_YES


def test_the_aperture_and_the_machine_are_different_ceilings(library, monkeypatch):
    """A personality can fit the GPU aperture and still not fit the box.

    98 GiB of loaded models inside a 100 GiB aperture, on a machine holding 110 GiB of RAM: the
    two llama-servers also hold their prompt caches (8192 MiB each by default) and, for a hybrid or
    sliding-window model, their context checkpoints. On this box GTT is a carve-out of the same
    physical RAM, so 98 + 16 does not fit in 110 even though 98 fits in 100.

    The seeded rows declare no hybrid or sliding-window layout, so no checkpoint cost is priced for
    them -- which is recorded in `host_notes` rather than silently reading as zero.
    """
    monkeypatch.setattr(mi, "ram_total_bytes", lambda: 110 * 2 ** 30)
    b = P.budget_for({"name": "x", "models_max": 2, "models": None},
                     budget_bytes=100 * 2 ** 30)
    assert b.worst_case_bytes == 98 * 2 ** 30
    assert b.fits is P.FITS_YES, "the aperture is not the problem being tested here"
    assert b.host_cache_bytes == 16 * 2 ** 30, "two servers x the 8192 MiB prompt cache"
    assert b.fits_ram is P.FITS_NO
    assert b.ram_headroom_bytes == -4 * 2 ** 30

    findings = P.validate({"name": "x", "models_max": 2, "models": None},
                          budget_bytes=100 * 2 ** 30)
    bad = [f for f in findings if f["severity"] == "bad"]
    assert len(bad) == 1, "the aperture fits, so the RAM ceiling is the only bad finding"
    assert "114.0 GiB" in bad[0]["message"] and "110.0 GiB" in bad[0]["message"], \
        "the refusal has to name the total that does not fit and the RAM it does not fit in"


def test_an_unbounded_prompt_cache_cannot_be_verified(library):
    """`cache-ram = -1` is a legal llama.cpp setting and an unpriceable request.

    The aperture verdict stays what it was -- the cache is not aperture -- while the RAM verdict
    becomes unknown rather than yes. A check that cannot measure must not report success.
    """
    per = {"name": "x", "models_max": 1, "models": None, "global": {"cache-ram": -1}}
    b = P.budget_for(per, budget_bytes=100 * 2 ** 30)
    assert b.fits is P.FITS_YES
    assert b.host_cache_bytes is None
    assert b.fits_ram is P.FITS_UNKNOWN
    warns = [f["message"] for f in P.validate(per, budget_bytes=100 * 2 ** 30)
             if f["severity"] == "warn"]
    assert any("no limit" in m for m in warns)


def test_models_max_that_does_not_fit_is_reported_with_its_numbers(library):
    """models_max=3 is 64 + 34 + 5 = 103 GiB, over an 80 GiB budget. Under 100 GiB it
    would also fail; this uses 80 so the message's two numbers differ clearly."""
    b = P.budget_for({"name": "x", "models_max": 3, "models": None},
                     budget_bytes=80 * 2**30)
    assert b.worst_case_bytes == 103 * 2**30
    assert b.fits is P.FITS_NO
    findings = P.validate({"name": "x", "models_max": 3, "models": None},
                          budget_bytes=80 * 2**30)
    bad = [f for f in findings if f["severity"] == "bad"]
    assert len(bad) == 1
    # The message must carry the numbers, or it is a warning an operator cannot act on.
    assert "103.0 GiB" in bad[0]["message"]
    assert "80.0 GiB" in bad[0]["message"]
    assert "huge" in bad[0]["message"]


def test_only_the_named_models_count_toward_the_budget(library):
    """A personality that names small+medium must not be judged against `huge`.
    Peaks: 34 + 5 = 39 GiB."""
    b = P.budget_for({"name": "x", "models_max": 2, "models": ["medium", "small"]})
    assert b.worst_case_bytes == 39 * 2**30
    assert b.heaviest == ["medium", "small"]


def test_an_unpriceable_model_makes_the_worst_case_unknown_not_safe(library):
    """est_total NULL means "we could not tell". Treating that as free would let a
    personality claim it fits while holding a model nobody priced."""
    _index_row("mystery", est_total=0, est_weights=None)
    conn = db._conn()
    conn.execute("UPDATE model_index SET est_total=NULL WHERE id='mystery'")
    conn.commit()

    b = P.budget_for({"name": "x", "models_max": 1, "models": ["mystery", "small"]})
    assert b.unpriced == ["mystery"]
    assert b.worst_case_bytes is None
    assert b.fits is P.FITS_UNKNOWN

    findings = P.validate({"name": "x", "models_max": 1, "models": ["mystery", "small"]})
    assert any(f["severity"] == "warn" and "unbounded" in f["message"] for f in findings)
    # The decisive one: unknown must not arrive as a green light.
    assert not any(f["severity"] == "ok" for f in findings)


def test_unreadable_budget_is_unknown_not_a_failure(library, monkeypatch):
    """modelindex._pool_bytes() falls back to 0, and 0 means "could not measure". If it
    flowed into the comparison, every personality would be reported as not fitting —
    the same collapse verdict() deliberately avoids."""
    monkeypatch.setattr(mi, "_pool_bytes", lambda: 0)
    b = P.budget_for({"name": "x", "models_max": 1, "models": None})
    assert b.budget_bytes is None
    assert b.fits is P.FITS_UNKNOWN
    findings = P.validate({"name": "x", "models_max": 1, "models": None})
    assert any(f["severity"] == "warn" and "no readable memory budget" in f["message"]
               for f in findings)
    assert not any(f["severity"] == "bad" for f in findings)


# ---------------------------------------------------------------- hostile input

@pytest.mark.parametrize("bad,expect_error", [
    ("three", True),      # int("three") raises ValueError with a message about literals
    ([1], True),
    ({}, True),
    (0, True),
    (-3, True),
    (None, False),        # absent means the default, 1
    (2, False),
    (2.7, False),         # int(2.7) == 2: truncation, but a whole number was intended
])
def test_models_max_surfaces_our_error_not_a_500(library, bad, expect_error):
    """describe() and render() both sit on a request path. A ValueError escaping here is
    an internal error page instead of a sentence naming the bad value."""
    per = {"name": "hostile", "models_max": bad, "models": ["small"]}
    if expect_error:
        with pytest.raises(P.PersonalityError):
            P.budget_for(per)
        d = P.describe(per)
        assert d["error"], "describe() must report it rather than half-build the dict"
        assert any(f["severity"] == "bad" for f in d["findings"])
    else:
        assert P.budget_for(per).models_max == (1 if bad is None else 2)


def test_describe_never_returns_a_clean_looking_personality_after_an_error(library):
    """The failure mode worth guarding: a dict with a valid models_max and an empty
    findings list reads as 'this personality is fine'."""
    d = P.describe({"name": "broken", "models_max": "many", "models": ["small"]})
    assert d["findings"], "must carry a finding"
    assert d["findings"][0]["severity"] == "bad"
    assert d["models"] == []


# ---------------------------------------------------------------- router-controlled keys

def test_router_controlled_keys_are_refused_not_ignored(library):
    """llama.cpp removes or overwrites host/port/alias/model/hf-repo on load. Emitting
    one would look like a setting and do nothing, which is worse than refusing it."""
    for key in ("port", "alias", "host", "model", "hf-repo", "api-key"):
        with pytest.raises(P.PersonalityError) as exc:
            P.render({"name": "x", "models_max": 1, "models": ["small"],
                      "overrides": {"small": {key: "whatever"}}})
        assert key in str(exc.value)


# ---------------------------------------------------------------- generated text

def test_generated_file_parses_the_way_the_router_reads_it(library):
    """Round-trip through the same parser family the router uses, and assert on the keys that
    are actually in the file.

    Values must come back as strings — `on` stays `on` rather than turning into a Python bool,
    because llama.cpp parses the token itself. `lazy-mode` is asserted because it is the key
    this box's memory behaviour depends on; `fit` is asserted *absent* because it was removed
    by hand and by measurement, and a generator that quietly re-adds it is the bug this test
    exists to catch.
    """
    import configparser
    text = P.render(P.builtins()[1])["text"]
    cp = configparser.ConfigParser(allow_no_value=True, strict=True,
                                   comment_prefixes=("#", ";"))
    cp.read_string(text)
    assert "*" in cp.sections()
    assert cp["*"]["lazy-mode"] == "on"
    assert cp["*"]["threads"] == "16"
    assert cp["*"]["stop-timeout"] == "15"
    assert "fit" not in cp["*"] and "fit-target" not in cp["*"]


def test_fit_keys_stay_out_of_every_generated_file(library):
    """`fit` is registered — the editor should be able to talk about it — but no builtin emits it.

    Three reasons, and the first one changed when §6.12.7 re-measured the question. `fit` is *not*
    blind to memory other processes hold on the device — it measures free device memory and does
    reduce the context; what it misses is host RAM and the compute graph it allocates after
    declaring success. What keeps these keys out of the generated file is that `fit`'s default is
    already `on` (so writing it says nothing), that it decides per load rather than against a plan,
    and that the owner had deleted both keys from the live file by hand.

    Written against `render()` rather than the preview route on purpose: the route needs an
    authenticated client and a library the shared model index agrees about, and this assertion is
    about the generator's own output. Cheaper to run and it cannot be contaminated by another
    file's fixture.
    """
    assert "fit" in P.SETTINGS and "fit-target" in P.SETTINGS
    assert "fit" not in P.GLOBAL_DEFAULTS, \
        "re-adding a hand-removed key is the portal overriding a better-evidenced decision"
    assert "fit-target" not in P.GLOBAL_DEFAULTS

    # Rendered from an explicit personality, not from a builtin: the builtins name models from
    # the real library, and this file's `library` fixture is a synthetic three-model tmp tree,
    # so render(safe) raises "no library entry" here for a reason that has nothing to do with
    # what this test is about.
    text = P.render({"name": "x", "models_max": 1, "models": ["small"]})["text"]
    assert "fit-target" not in text
    assert "\nfit = " not in text
    assert "lazy-mode = on" in text, \
        "the managed router gets --lazy-mode from nowhere else, so the generated file is its only source"


# ------------------------------------------------ settings that would say nothing

def test_no_personality_limits_a_context_the_author_did_not_choose(library):
    """Nothing the portal writes may cap a context unless somebody asked for that cap.

    The tree this box started from capped every model at `ctx-size = 65536` -- models trained at
    262144 and one trained at 1048576 alike. A cap that low is invisible capability loss: the model,
    the page and the user all agree the model is shorter than it is, and the file never says who
    decided. So the absence of `ctx-size` is the policy, and it needs a guard rather than a habit:
    no builtin carries an override, no global default carries the key, and a personality that lists
    models without overrides renders no `ctx-size` line at all.

    Shrinking still happens -- llama.cpp's `fit` does it at load when it has no choice (§6.12.7).
    The difference is that the portal reports that instead of causing it.
    """
    assert "ctx-size" not in P.GLOBAL_DEFAULTS, \
        "a global ctx-size caps every model in the library at once"
    for b in P.builtins():
        for model, per in (b.get("overrides") or {}).items():
            assert "ctx-size" not in per, \
                f"builtin {b['name']!r} caps {model} at {per.get('ctx-size')}"
    text = P.render({"name": "x", "models_max": 3,
                     "models": ["small", "medium", "huge"]})["text"]
    assert "ctx-size" not in text


def test_an_explicit_context_choice_survives_everything_else(library):
    """The other half of the rule: when an operator does choose, the choice is written verbatim.

    A guard that only proves "we never cap" would be satisfied by a generator that dropped ctx-size
    on the floor, which is the same bug wearing the opposite clothes.
    """
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "overrides": {"small": {"ctx-size": 2048}}})
    assert "ctx-size = 2048" in out["text"]
    assert out["omitted"] == []


def test_a_model_scoped_key_cannot_be_set_for_every_model_at_once(library):
    """`[*]` reaches every model the router *discovers*, not the models a personality listed.

    Measured on this box (§4.10.2): a preset carrying `[*] load-on-startup = on` began loading a
    local 0.8B model *and* `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL` found through the shared HF cache --
    a 27 GiB model this personality had never named -- and `models-max = 1` in the same file did not
    stop either. `ctx-size` in `[*]` is the same shape of harm: one line caps every model in the
    library, which is exactly what §4.10.1 forbids.
    """
    for key, value in (("load-on-startup", True), ("ctx-size", 4096)):
        with pytest.raises(P.PersonalityError) as exc:
            P.render({"name": "x", "models_max": 1, "models": ["small"], "global": {key: value}})
        assert f"{key}" in str(exc.value) and "[*]" in str(exc.value)
        assert "own section" in str(exc.value), \
            "the refusal has to say where the value belongs, not just that it is wrong"


def test_startup_loading_is_a_per_model_choice_and_the_negative_form_says_nothing(library):
    """`on` is a decision; `off` restates what happens when nothing is written.

    A router with a preset that set nothing produced zero startup loads, and the same router with
    `load-on-startup = off` produced zero startup loads (§4.10.2). The false is therefore the unset
    state and belongs to the no-op rule, while `on` -- per model, in that model's section -- is the
    only way a personality decides what is resident before the first request.
    """
    on = P.render({"name": "x", "models_max": 1, "models": ["small"],
                   "overrides": {"small": {"load-on-startup": True}}})
    assert "load-on-startup = on" in on["text"]

    off = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "overrides": {"small": {"load-on-startup": False}}})
    # `= ` and not the bare key: the file's own header names the router-only keys, and a guard that
    # matches the header would pass while the setting was present, or fail while it was absent.
    assert "load-on-startup = " not in off["text"]
    assert [o["key"] for o in off["omitted"]] == ["load-on-startup"]


def test_global_defaults_are_all_real_choices_not_copies_of_the_servers(library):
    """Nothing in the shipped global section may be a restatement of llama-server's own default.

    Such a line looks like a decision and is really a snapshot of one release's number; the day
    upstream changes it, the pinned copy becomes the setting and nobody remembers agreeing to it.
    """
    copied = {k: v for k, v in P.GLOBAL_DEFAULTS.items() if P._no_op_reason(k, v, None)}
    assert copied == {}, f"GLOBAL_DEFAULTS restates the server's defaults: {copied}"


def test_no_setting_is_defined_twice_in_the_registry():
    """A duplicate key in a dict literal is not an error. The last definition simply wins.

    `ctx-checkpoints` was defined three times in SETTINGS, and the one that took effect was the
    tersest -- "Costs memory per slot" -- silently replacing a paragraph that carried the measured
    per-checkpoint sizes and the fact that the cost is host RAM rather than aperture. Nothing broke,
    because the registry was still a valid dictionary with the key in it. That is what makes this
    worth a test rather than a review comment.
    """
    import ast
    import collections
    import pathlib

    tree = ast.parse(pathlib.Path(P.__file__).read_text())
    offenders: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            names = [node.target.id] if isinstance(node.target, ast.Name) else []
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        else:
            continue
        for name in names:
            if name not in ("SETTINGS", "SERVER_DEFAULTS", "GLOBAL_DEFAULTS"):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            dupes = sorted({k for k, c in collections.Counter(keys).items() if c > 1})
            if dupes:
                offenders[name] = dupes
    assert offenders == {}, f"later definitions silently overwrote earlier ones: {offenders}"


#: Settings whose description already states their whole effect, and why that is sufficient.
#: A bare exemption list is how the failure above becomes invisible again, so each entry has to
#: carry its reason and the test refuses entries that stop being needed.
EFFECT_IS_THE_NAME = {
    "threads": "Sizes the CPU thread pool used during generation; nothing else in a plan is derived "
               "from it, and it cannot change what fits.",
}


def test_every_setting_states_the_effects_the_name_does_not(library):
    """Capacity settings that act sideways have to say so where the operator is reading.

    `fit-target` sounds like a safety margin and is really "shrink the context sooner, and reserve
    aperture while the caches live in host RAM". `n-gpu-layers` sounds like a placement choice and is
    really "convert reclaimable weight pages into unreclaimable anonymous RAM". The description the
    editor renders is the only place either of those is discoverable without reading the plan.
    """
    missing = {k for k, spec in P.SETTINGS.items()
               if not spec.get("side_effects") and k not in EFFECT_IS_THE_NAME}
    assert missing == set(), f"settings whose consequences are not stated: {missing}"
    stale = {k for k in EFFECT_IS_THE_NAME if P.SETTINGS.get(k, {}).get("side_effects")}
    assert stale == set(), f"described after all, remove from EFFECT_IS_THE_NAME: {stale}"
    absent = {k for k in EFFECT_IS_THE_NAME if k not in P.SETTINGS}
    assert absent == set(), f"exempted keys that are not settings at all: {absent}"


def test_the_no_op_rule_covers_every_writable_setting_with_a_known_default(library):
    """The table has to cover what the editor can write, not what was remembered when it was written.

    The gap is asserted as an explicit set rather than as "no gaps", because two settings genuinely
    have no verified server-side default: `dedup-cache-models` is implemented in the router's preset
    reader and was not measured here, and `load-on-startup` is absent on purpose -- the router
    autoloads unless a preset says otherwise, so writing `load-on-startup = false` is a decision, not
    a restatement. A new setting with no entry here fails this test, which is the point: an
    uncovered key can be written as a silent no-op and nobody would notice.
    """
    gaps = {k for k, spec in P.SETTINGS.items()
            if spec["section"] in ("both", "model") and k not in P.SERVER_DEFAULTS}
    assert gaps == {"dedup-cache-models"}, \
        f"settings the editor can write with no entry in SERVER_DEFAULTS: {gaps}"


def test_the_settings_that_ship_at_their_default_produce_no_line(library):
    """Every one of these is what llama.cpp already does, so writing them is noise with a
    provenance problem: the line is a snapshot of one release's number.

    `fit = on` is the case this rule exists for. The live presets.ini on this box used to carry it
    while the server's own default is `on` -- a line that looked like a decision and changed nothing.
    """
    per = {"name": "x", "models_max": 1, "models": ["small"],
           "global": {"fit": "on", "fit-target": 1024, "cache-ram": 8192,
                      "ctx-checkpoints": 32, "checkpoint-min-step": 8192,
                      "cache-idle-slots": True, "keep": 0}}
    out = P.render(per)
    for key in ("fit", "fit-target", "cache-ram", "ctx-checkpoints",
                "checkpoint-min-step", "cache-idle-slots", "keep"):
        assert f"{key} = " not in out["text"], f"{key} was written despite being the server's default"
    assert {o["key"] for o in out["omitted"]} == set(per["global"]), \
        "every suppressed value has to be reported, not quietly dropped"


def test_a_deliberate_margin_above_the_default_is_still_written(library):
    """The rule suppresses restatements, not decisions. `fit-target = 8192` is a 8 GiB aperture
    margin instead of 1 GiB -- that is a change worth a line, and worth keeping visible."""
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "global": {"fit-target": 8192, "fit": "on"}})
    assert "fit-target = 8192" in out["text"]
    assert "fit = " not in out["text"]
    assert [o["key"] for o in out["omitted"]] == ["fit"]


def test_a_value_equal_to_the_server_default_is_omitted_and_explained(library):
    """Omitted, and reported. An operator who set a value and finds it absent from the file has
    to be able to learn why, from the same response that showed them the diff."""
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "global": {"threads": -1, "flash-attn": "auto"}})
    assert "threads = " not in out["text"] and "flash-attn" not in out["text"]
    reasons = {o["key"]: o["reason"] for o in out["omitted"]}
    assert set(reasons) == {"threads", "flash-attn"}
    assert "default" in reasons["threads"]


def test_ctx_size_at_the_trained_maximum_is_the_same_as_not_setting_it(library):
    """The reflex case: an editor being explicit about a model's limit writes the limit, which
    is what the server does anyway. The whole section goes with it — a header with no settings
    under it would make the router report a custom preset that sets nothing."""
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "overrides": {"small": {"ctx-size": 4096}}})
    assert "ctx-size" not in out["text"]
    assert "[small]" not in out["text"], "a section emptied by omission must not survive"
    assert out["omitted"][0]["reason"].startswith(
        "ctx-size = 4096 is this model's trained maximum")


def test_ctx_size_zero_is_the_same_as_not_setting_it(library):
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "overrides": {"small": {"ctx-size": 0}}})
    assert "ctx-size" not in out["text"]
    assert any("loaded from the model" in o["reason"] for o in out["omitted"])


def test_a_ctx_size_that_is_a_real_choice_still_gets_written(library):
    out = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "overrides": {"small": {"ctx-size": 2048}}})
    assert "[small]" in out["text"] and "ctx-size = 2048" in out["text"]
    assert out["omitted"] == []


def test_a_smaller_context_is_never_priced_below_the_stored_estimate(library):
    """The fallback clamps to the stored estimate because scaling down is the unsafe direction.

    Measured on this box, not theorised: `gemma-4-31B-it-QAT-Q4_0` is priced at 7.075 GiB of cache
    at 262144, and at 8192 it really costs 0.718 GiB, while linear scaling reports 0.221 GiB — 3.2×
    too small. Cache size is concave in ctx (window-capped layers hold min(ctx, window)), so the
    chord from the origin to the stored point sits *below* the truth beneath that point.

    `huge` here is priced at 5 GiB of cache at 4096. Asking for 2048 keeps the 5 GiB rather than
    halving it: 55 + 5 + 4 = 64 GiB. Asserting the arithmetic is the point — a fallback that
    quietly under-priced would otherwise be invisible in a test that only asked whether it passed.
    """
    row = next(r for r in mi.rows() if r["id"] == "huge")
    assert P._conservative_peak(row, 2048) == P._conservative_peak(row, 4096), \
        "a smaller context was priced below the estimate made at the larger one"
    assert P._conservative_peak(row, 2048) == 64 * 2 ** 30

    out = P.render({"name": "x", "models_max": 1, "models": ["huge"],
                    "overrides": {"huge": {"ctx-size": 2048}}},
                   budget_bytes=100 * 2 ** 30)
    assert "ctx-size = 2048" in out["text"]
    assert out["budget"]["worst_case_bytes"] == 64 * 2 ** 30


def test_ctx_size_above_the_models_trained_maximum_is_refused(library):
    """Not a warning. presets.ini is read at load time by something that will not explain itself,
    and a context larger than the model was trained for is a load the router will fail."""
    with pytest.raises(P.PersonalityError) as exc:
        P.render({"name": "x", "models_max": 1, "models": ["small"],
                  "overrides": {"small": {"ctx-size": 8192}}})
    assert "trained for 4096" in str(exc.value)


def test_ctx_size_whose_cache_cannot_be_paid_for_is_refused(library):
    """Within the model's own limit and still beyond the aperture: two different ceilings, and a
    value can clear one and fail the other.

    est_ctx is re-seeded to 2048 so peak_bytes() prices this row from its stored est_kv instead
    of deriving KV from attention geometry a synthetic row does not have. Left at the fixture
    default it returns None and the refusal under test never runs.
    """
    _index_row("huge", est_total=60 * 2 ** 30, est_weights=55 * 2 ** 30,
               est_ctx=2048, ctx_trained=4096)
    with pytest.raises(P.PersonalityError) as exc:
        P.render({"name": "x", "models_max": 1, "models": ["huge"],
                  "overrides": {"huge": {"ctx-size": 2048}}},
                 budget_bytes=10 * 2 ** 30)
    assert "64.0 GiB against a 10.0 GiB aperture" in str(exc.value), \
        "the refusal has to carry the two numbers that produced it"


def test_an_unpriceable_ctx_size_is_refused_not_allowed(library):
    """A model the index cannot price must not pass an aperture check that has nothing to measure.

    `mystery` carries no weight estimate, which is the state peak_bytes() already treats as
    unpriceable; the conservative fallback refuses for the same reason instead of substituting a
    number it was never given.
    """
    _index_row("mystery", est_total=8 * 2 ** 30, est_weights=0)
    with pytest.raises(P.PersonalityError) as exc:
        P.render({"name": "x", "models_max": 1, "models": ["mystery"],
                  "overrides": {"mystery": {"ctx-size": 2048}}},
                 budget_bytes=10 * 2 ** 30)
    assert "cannot be priced" in str(exc.value)


def test_booleans_are_written_the_way_llama_cpp_reads_them(library):
    """`True` is Python's spelling and the preset parser is not Python.

    Both directions are asserted: a non-default boolean is written as on/off, and a default one
    is dropped rather than written in either spelling.
    """
    off = P.render({"name": "x", "models_max": 1, "models": ["small"],
                    "global": {"cache-prompt": False}})
    assert "cache-prompt = off" in off["text"]
    assert "True" not in off["text"] and "False" not in off["text"]

    same_as_default = P.render({"name": "x", "models_max": 1, "models": ["small"],
                                "global": {"cache-prompt": True}})
    assert "cache-prompt" not in same_as_default["text"]
    assert any(o["key"] == "cache-prompt" for o in same_as_default["omitted"])


def test_global_section_comes_first_because_the_router_applies_it_as_the_base(library):
    """Precedence is CLI > model section > [*]. Position in the file does not decide
    that, but a human reading the file reasons top-down, and [*] first matches how the
    values actually resolve.

    2048, not 8192: every seeded row here is trained to 4096 and render() now refuses a context
    above that. Ordering is what this test is about, so its example has to be a legal value.
    """
    text = P.render({"name": "x", "models_max": 1, "models": ["small"],
                     "overrides": {"small": {"ctx-size": 2048}}})["text"]
    assert text.index("[*]") < text.index("[small]")


def test_diff_against_the_file_on_disk_is_reported(library, monkeypatch, tmp_path):
    """N4's contract is generate -> show a diff -> apply. The diff is the safety step,
    so it has to be present and non-empty when something actually changes."""
    real = tmp_path / "presets.ini"
    real.write_text("[*]\nthreads = 4\n")
    monkeypatch.setattr(P.paths, "PRESETS_INI", real)

    out = P.render({"name": "x", "models_max": 1, "models": ["small"]})
    assert out["changed"] is True
    assert any(ln.startswith("+threads = 16") for ln in out["diff"])
    assert any(ln.startswith("-threads = 4") for ln in out["diff"])


def test_render_is_read_only(library, monkeypatch, tmp_path):
    """The one assertion that must not be relaxed: N4 is the first step in this project
    that changes anything the router reads, so generation must not write. Writing is a
    separate call that has to go through the gated restart job and record history first.
    """
    real = tmp_path / "presets.ini"
    real.write_text("[*]\nthreads = 4\n")
    before = real.read_text()
    monkeypatch.setattr(P.paths, "PRESETS_INI", real)

    P.render({"name": "x", "models_max": 2, "models": None})
    P.validate({"name": "x", "models_max": 2, "models": None})
    P.describe(P.builtins()[1])
    P.parse_current()

    assert real.read_text() == before, "render/validate/describe must not write presets.ini"


def test_parse_current_degrades_on_a_broken_file_instead_of_taking_the_page_down(library,
                                                                                 monkeypatch,
                                                                                 tmp_path):
    """A presets.ini the router cannot read is a real state this box has been in. The
    personality page reporting {} is a degraded card; an exception is a 500."""
    bad = tmp_path / "presets.ini"
    bad.write_text("[*]\nthreads = 4\nctx\n-size\n \n=\n 8192\n")
    monkeypatch.setattr(P.paths, "PRESETS_INI", bad)
    assert P.parse_current() == {}


def test_parse_current_reads_a_healthy_file(library, monkeypatch, tmp_path):
    good = tmp_path / "presets.ini"
    good.write_text("[*]\nthreads = 16\n\n[small]\nctx-size = 8192\n")
    monkeypatch.setattr(P.paths, "PRESETS_INI", good)
    got = P.parse_current()
    assert got["*"]["threads"] == "16"
    assert got["small"]["ctx-size"] == "8192"


# ---------------------------------------------------------------- the two permanent ones

def test_safe_and_default_exist_and_are_not_editable_in_place(library):
    """§4.7: if `safe` cannot start the box is broken, and an edit that bypasses a
    personality is how the next person debugging an OOM loses their afternoon."""
    names = [p["name"] for p in P.builtins()]
    assert names == ["safe", "default"]
    for per in P.builtins():
        d = P.describe(per)
        assert d["editable"] is False


def test_safe_personality_is_one_model_and_no_per_model_overrides(library):
    safe = P.builtins()[0]
    assert safe["models_max"] == 1
    assert len(safe["models"]) == 1
    assert not safe.get("overrides")


def test_default_personality_carries_no_per_model_overrides(library):
    """default inherits from [*] only. The per-model overrides a user can reach are the
    ones the portal deliberately exposes, so an implicit one here would be an unlisted
    edit to presets.ini."""
    assert not P.builtins()[1].get("overrides")


def test_generated_output_is_deterministic(library):
    """Ordering must not depend on dict iteration, or every diff shows churn that is not
    a decision."""
    per = {"name": "x", "models_max": 2, "models": None}
    assert P.render(per)["text"] == P.render(per)["text"]


# ------------------------------------------- refusing, and saying what to use instead
def _avail_rows() -> list[dict]:
    return [
        {"id": "orga/Qwen3.5-0.8B-GGUF:Q4_K_M", "bytes": 512 * 1024**2, "pooling_type": None},
        {"id": "ggml-org/embeddinggemma-300M-GGUF:Q8_0", "bytes": 320 * 1024**2,
         "pooling_type": 1},
        {"id": "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF:Q8_0", "bytes": 610 * 1024**2,
         "pooling_type": 4},
        {"id": "lmstudio-community/codegemma-2b-GGUF:Q5_K_M", "bytes": 1790 * 1024**2,
         "pooling_type": None},
    ]


def test_a_missing_target_is_still_a_refusal_and_now_names_what_exists(tmp_path, monkeypatch):
    """The refusal must not become a substitution.

    Guessing a replacement model would write a preset for weights the operator never chose. The
    help here is a list of candidates next to a refusal that still refuses.
    """
    monkeypatch.setattr(P, "_rows", lambda: _avail_rows())
    person = {"name": "safe", "models": ["Qwen3.5-0.8B.Q4_K_M"]}
    try:
        P.render(person, 100 * 1024**3)
        raise AssertionError("an absent model must still be refused")
    except P.PersonalityError as exc:
        message = str(exc)
    assert "no library entry and no stored config: Qwen3.5-0.8B.Q4_K_M" in message
    assert "closest by name: orga/Qwen3.5-0.8B-GGUF:Q4_K_M" in message


def test_the_size_list_only_offers_models_that_can_hold_the_slot():
    """The two smallest rows in the real index are an embedder and a reranker.

    Offering them for a chat slot is the kind of helpful-looking wrong answer this portal keeps
    getting bitten by: cheap, indexed, and unable to serve a completion.
    """
    sug = P.suggest_for("nothing-remotely-like", _avail_rows())
    assert [s["id"] for s in sug["smallest"]] == [
        "orga/Qwen3.5-0.8B-GGUF:Q4_K_M", "lmstudio-community/codegemma-2b-GGUF:Q5_K_M"]
    assert sug["smallest"][0]["bytes"] == 512 * 1024**2   # ascending, and neither is a pooler


def test_an_empty_index_says_so_rather_than_listing_nothing():
    monkeypatch_rows = P._rows
    try:
        P._rows = lambda: []
        message = P._availability("Qwen3.5-0.8B.Q4_K_M")
    finally:
        P._rows = monkeypatch_rows
    assert "model index is empty" in message
