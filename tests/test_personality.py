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

    `fit_probe.py` measured two things that make it unsafe as a default: `fit` is blind to memory
    other processes already hold (18 GB held elsewhere changed the context it chose by nothing at
    all), and it does not bound the model it is applied to (an 87.2 GB model occupied ~98 GB under
    `--fit-target 8192`). The owner had already deleted both keys from the live file by hand.

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


def test_global_section_comes_first_because_the_router_applies_it_as_the_base(library):
    """Precedence is CLI > model section > [*]. Position in the file does not decide
    that, but a human reading the file reasons top-down, and [*] first matches how the
    values actually resolve."""
    text = P.render({"name": "x", "models_max": 1, "models": ["small"],
                     "overrides": {"small": {"ctx-size": 8192}}})["text"]
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
