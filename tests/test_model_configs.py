"""Model configs and custom personalities (PORTAL_PLAN §4.10.4).

Two things are being tested here and they are not the same thing:

  * the *store* — what may be created, renamed, reused, deleted, and what refuses;
  * the *effect* — the file the router would actually read, and the name it would answer to.

The effect tests assert against generated text rather than against a dict, because the thing that
gets loaded is the file. Where a claim depends on router behaviour it is quoted from a measurement
on this box (§4.10.4), not from documentation.
"""
from __future__ import annotations

import os
import re
import tempfile
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
# Set at import, like every other module here, and for the reason in tests/conftest.py: `paths` froze
# its home when it was imported, not when a fixture ran. This module is the one that got it wrong the
# first time it existed.
os.environ.setdefault("AI_PORTAL_HOME", str(Path(tempfile.gettempdir()) / "aiportal-model-configs"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.core import config as cfg  # noqa: E402
from app.core import db, model_config as MC, paths, presets_store  # noqa: E402
from app.core import modelindex as mi  # noqa: E402
from app.core import personality as P  # noqa: E402
from tests.sandbox import clear_library, clear_snapshots  # noqa: E402
from tests.test_modelindex import _write_fake_gguf  # noqa: E402

BUDGET = 64 * 1024 ** 3
ORIGINAL = "[*]\nthreads = 16\nstop-timeout = 15\n"

META = {"block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
        "embedding_length": 256, "context_length": 4096}


def _gguf(path: Path, size: int) -> None:
    """A priceable GGUF of a given size, by truncation rather than padding (see
    tests/test_presets_apply.py: `pad` materialises the bytes, and /tmp is tmpfs)."""
    path.parent.mkdir(parents=True, exist_ok=True)   # the fixture writer does not make directories
    _write_fake_gguf(path, dict(META))
    with path.open("r+b") as fh:
        fh.truncate(size)


@pytest.fixture()
def lib():
    """A clean library, an empty config table, an empty personality table, a known presets.ini.

    All four are cleared because all four are shared: this suite has one model_index, one
    model_configs and one personalities table for the whole process, and a leftover from another
    module shows up as a model or a personality this test never created — which for a budget
    assertion is indistinguishable from a wrong calculation.
    """
    db.init()
    mi.init()
    # Through tests/sandbox.py rather than a local rmtree: clearing the library is the one thing in
    # this module that could reach a real one, and it is the thing that already has.
    clear_library()
    conn = db._conn()
    for table in ("model_index", "model_configs", "personalities"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    _gguf(paths.LLAMA_MODELS / "Alpha-7B" / "Alpha-7B.Q4_K_M.gguf", 4 * 1024 ** 3)
    _gguf(paths.LLAMA_MODELS / "Beta-13B" / "Beta-13B.Q4_K_M.gguf", 8 * 1024 ** 3)
    mi.rescan()

    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_INI.write_text(ORIGINAL)
    # Through the sandbox as well: snapshots are the other tree a test can clear, and a leftover
    # snapshot makes a rollback test restore bytes some earlier test wrote.
    clear_snapshots()

    yield {"budget": BUDGET}

    conn = db._conn()
    for table in ("model_index", "model_configs", "personalities"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _keys(text: str) -> list[str]:
    """Every key the generated file actually sets -- comments and section headers excluded.

    The assertion this exists for is "this personality emits no `alias`". Testing that against the
    whole text fails for the wrong reason: the file's own header explains that a model section *may*
    set `alias`, so the word appears in every generated file. A check that fires on prose is a check
    that will be deleted rather than fixed.
    """
    keys = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" in stripped:
            keys.append(stripped.split("=", 1)[0].strip())
    return keys


def _config(name="fast-alpha", base="Alpha-7B", settings=None, note=""):
    clean = P.normalize_config(name, base, settings if settings is not None
                               else {"ctx-size": "2048", "parallel": "2"}, note)
    return MC.save(clean["name"], clean["base_model"], clean["settings"], clean["note"])


# ---------------------------------------------------------------- the generated file

def test_a_config_renames_its_model_in_the_file_the_router_reads(lib):
    """The config's name becomes `alias`, and the section stays keyed by the directory name.

    Both halves matter. A section called `[fast-alpha]` would be ignored by the router (sections
    match directories), and a section called `[Alpha-7B]` with no alias would apply the settings
    while the model stayed under the name the file shipped with — half of what a config promises.
    """
    _config()
    P.save_personality("work", 1, "", ["fast-alpha"], {})
    text = P.render(P.find("work"), BUDGET)["text"]

    assert "[Alpha-7B]" in text, text
    assert "alias = fast-alpha" in text, text
    assert "[fast-alpha]" not in text, text


def test_a_bare_model_entry_is_the_file_under_its_own_name_with_nothing_added(lib):
    """Naming a model directly is not the same as naming a config, and must not become one.

    `default` and `safe` are made entirely of these. If a bare entry acquired a section, editing
    personalities would quietly reach the models `default` covers.
    """
    out = P.render(P.builtins()[1], BUDGET)
    assert "alias" not in _keys(out["text"]), _keys(out["text"])
    assert "[Alpha-7B]" not in out["text"] and "[Beta-13B]" not in out["text"], out["text"]


def test_a_config_that_no_personality_assigns_changes_nothing(lib):
    """Configs are not applied by existing. They are applied by a personality choosing them."""
    _config()
    text = P.render(P.builtins()[1], BUDGET)["text"]
    assert "fast-alpha" not in text, text


def test_config_settings_land_under_the_model_they_came_from(lib):
    """Settings travel with the config, not with the personality that happens to use it."""
    _config(settings={"ctx-size": "2048", "flash-attn": "on"})
    P.save_personality("work", 1, "", ["fast-alpha", "Beta-13B"], {})
    sections = P._reparse(P.render(P.find("work"), BUDGET)["text"])
    assert sections["Alpha-7B"]["ctx-size"] == "2048"
    assert sections["Alpha-7B"]["flash-attn"] == "on"
    assert "Beta-13B" not in sections, sections


def test_the_layering_is_global_then_config_then_personality_override(lib):
    """Three layers can name the same key. The order is a rule, so it is asserted, not implied.

    The personality override layer is the one the page no longer creates; it survives for payloads
    assembled by hand, and a precedence that only exists when nobody looks at it is the kind that
    gets "fixed" by accident.

    `[*]` cannot carry a model-scoped key at all -- measured §4.10.2, and refused since 1c4836c -- so
    the global layer is exercised with a global key. The two layers that can both name one model's
    `ctx-size` are the config and the override, and their order is what the test is for.
    """
    _config(settings={"ctx-size": "2048"})
    persona = {"name": "work", "models_max": 1, "models": ["fast-alpha"],
               "global": {"threads": 8}, "overrides": {"Alpha-7B": {"ctx-size": 3072}}}
    sections = P._reparse(P.render(persona, BUDGET)["text"])
    assert sections["Alpha-7B"]["ctx-size"] == "3072", sections      # override over config
    # GLOBAL_DEFAULTS ships threads = 16, so 8 here can only have come from this personality.
    assert sections["*"]["threads"] == "8", sections

    persona.pop("overrides")
    sections = P._reparse(P.render(persona, BUDGET)["text"])
    assert sections["Alpha-7B"]["ctx-size"] == "2048", sections      # the config is the floor

    # And the global layer cannot reach into the model's section to supply that key instead.
    persona["global"]["ctx-size"] = 1024
    with pytest.raises(P.PersonalityError) as exc:
        P.render(persona, BUDGET)
    assert "['ctx-size'] in [*]" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------- the store's refusals

@pytest.mark.parametrize("name,base,why", [
    # Measured, §4.10.4: a colliding alias is not a warning. The router exits 1 with
    # "model name '...' conflicts with alias of model '...'" and loads nothing.
    ("Beta-13B", "Alpha-7B", "already a model in the library"),
    ("fast alpha", "Alpha-7B", "letters, digits, dot, dash and underscore"),
    ("fast,alpha", "Alpha-7B", "letters, digits, dot, dash and underscore"),
    # `*` never reaches this check -- the charset rule fires first -- so the reserved case is a name
    # that passes the charset and is still spoken for: the global section and the two built-ins.
    ("default", "Alpha-7B", "is reserved"),
    ("safe", "Alpha-7B", "is reserved"),
    ("Alpha-7B", "Alpha-7B", "already a model in the library"),
    ("x" * 49, "Alpha-7B", "at most 48 characters"),
])
def test_a_name_the_router_would_reject_is_refused_before_anything_is_written(lib, name, base,
                                                                              why):
    """Every refusal here exists because the alternative is a router that will not start."""
    with pytest.raises((MC.ConfigError, P.PersonalityError)) as exc:
        P.normalize_config(name, base, {"ctx-size": "1024"})
        MC.save(name, base, {"ctx-size": 1024})
    assert why in str(exc.value), str(exc.value)


def test_a_second_config_cannot_reuse_a_name(lib):
    """Names are one namespace, so a second config for a different model needs a different name."""
    _config("fast-alpha", "Alpha-7B")
    with pytest.raises(MC.ConfigError) as exc:
        _config("fast-alpha", "Beta-13B")
    assert "already configures" in str(exc.value)


def test_a_config_does_not_follow_a_personality_to_another_model(lib):
    """Retargeting is refused rather than tracked.

    A config whose base model changed would keep its name and its settings while pointing at a
    different file — so every personality that assigned `fast-alpha` would start meaning the other
    model, with nothing in the personality to explain the change.
    """
    _config("fast-alpha", "Alpha-7B")
    with pytest.raises(MC.ConfigError) as exc:
        MC.save("fast-alpha", "Beta-13B", {"ctx-size": 1024})
    assert "does not change which model it names" in str(exc.value)


def test_two_configs_on_one_model_are_refused_not_reordered(lib):
    """One preset section per model, so two configs for the same file has no legal meaning.

    Deciding this by dictionary order would produce a file that looks like both took effect.
    """
    _config("fast-alpha", "Alpha-7B", {"ctx-size": "1024"})
    _config("quiet-alpha", "Alpha-7B", {"ctx-size": "2048"})
    with pytest.raises(P.PersonalityError) as exc:
        P.save_personality("work", 1, "", ["fast-alpha", "quiet-alpha"], {})
    assert "same model" in str(exc.value)


def test_a_personality_that_cannot_be_generated_is_not_saved(lib):
    """Refuse at save, not at Apply.

    A stored personality that cannot be rendered is a card on the page that errors every time it
    is looked at, and an operator finds that out by trying to use it.
    """
    with pytest.raises(P.PersonalityError) as exc:
        P.save_personality("work", 1, "", ["wide"], {})      # 'wide' does not exist yet
    assert "no library entry" in str(exc.value)

    _config("wide", "Alpha-7B", {"ctx-size": "999999"})      # beyond the trained 4096
    with pytest.raises(P.PersonalityError) as exc:
        P.save_personality("work", 1, "", ["wide"], {})
    assert "trained for 4096" in str(exc.value)


def test_deleting_a_config_out_from_under_a_personality_is_refused(lib):
    """The failure being prevented is silent: the personality keeps its name and loses its settings.

    Nothing downstream errors. The next apply simply writes a smaller file than the operator
    believes they are editing.
    """
    _config()
    P.save_personality("work", 1, "", ["fast-alpha"], {})
    assert MC.assigned_personalities("fast-alpha") == ["work"]
    with pytest.raises(MC.ConfigError):
        MC.save("fast-alpha", "Beta-13B", {})                # retarget: refused
    assert MC.delete("fast-alpha") is True                    # storage-level delete
    with pytest.raises(P.PersonalityError) as exc:
        P.render(P.find("work"), BUDGET)
    assert "no stored config" in str(exc.value)


def test_models_none_means_the_whole_library_for_a_custom_personality_too(lib):
    """`None` is a library-wide personality, not an empty one — and it stays config-free."""
    P.save_personality("everything", 2, "", None, {"threads": "8"})
    out = P.render(P.find("everything"), BUDGET)
    assert sorted(out["models"]) == ["Alpha-7B", "Beta-13B"]
    assert "alias" not in _keys(out["text"]), _keys(out["text"])
    assert out["budget"]["models_max"] == 2


def test_config_settings_are_what_reprice_the_budget(lib):
    """A config is not only a name: the context it sets is what the budget has to pay for.

    The base file's own estimate prices the model at its default context; the config asks for a
    smaller one, so the worst case for this personality must move.
    """
    base = P.render(P.builtins()[1], BUDGET)["budget"]["worst_case_bytes"]
    _config("tiny", "Beta-13B", {"ctx-size": "1024"})
    P.save_personality("small", 1, "", ["tiny"], {})
    priced = P.render(P.find("small"), BUDGET)["budget"]
    assert priced["worst_case_bytes"] < base, (priced, base)


# ---------------------------------------------------------------- built-ins stay fixed

def test_safe_and_default_cannot_be_saved_over_or_deleted(lib):
    for name in ("safe", "default"):
        with pytest.raises(P.PersonalityError) as exc:
            P.save_personality(name, 1, "attempted", ["Alpha-7B"], {})
        assert "built-in personality" in str(exc.value)
        with pytest.raises(P.PersonalityError):
            P.delete_personality(name)


def test_describe_says_which_personalities_are_editable_and_which_name_a_config(lib):
    _config()
    P.save_personality("work", 1, "", ["fast-alpha"], {})
    described = {d["name"]: d for d in (P.describe(p, BUDGET) for p in P.all_personalities())}
    assert described["safe"]["editable"] is False and described["safe"]["built_in"] is True
    assert described["work"]["editable"] is True and described["work"]["built_in"] is False
    assert described["work"]["configs"] == [{"name": "fast-alpha", "model": "Alpha-7B"}]
    assert described["safe"]["configs"] == []
    # The model list is base models throughout, so a budget and a model count mean the same
    # thing for a config personality and for `default`.
    assert described["work"]["models"] == ["Alpha-7B"]


def test_a_stored_personality_survives_a_reload(lib):
    """Stored means stored: JSON columns are decoded on read, not remembered in memory."""
    P.save_personality("work", 3, "three at once", ["Alpha-7B", "Beta-13B"], {"threads": "8"})
    again = P.find("work")
    assert again["models_max"] == 3 and again["note"] == "three at once"
    assert again["models"] == ["Alpha-7B", "Beta-13B"]
    assert again["global"] == {"threads": 8}


# ------------------------------------------------- the file-level guard on aliases

def test_check_generated_accepts_a_sound_alias_and_refuses_the_shapes_that_break_the_router(lib):
    """`alias` is the one router-controlled key the portal writes, so the last guard has to know
    what a sound one is: in a model section, a usable name, not a model's own name, and not
    repeated for a second model. Measured behaviour, §4.10.4."""
    known = {r["id"] for r in mi.rows()}

    ok = "[Alpha-7B]\nalias = fast-alpha\nctx-size = 2048\n"
    assert P.check_generated(ok, known) == [], P.check_generated(ok, known)

    in_global = P.check_generated("[*]\nalias = fast-alpha\n", known)
    assert any("[*] alias" in p for p in in_global), in_global

    collides = P.check_generated("[Alpha-7B]\nalias = Beta-13B\n", known)
    assert any("already a model in the library" in p for p in collides), collides

    twice = P.check_generated("[Alpha-7B]\nalias = same\n[Beta-13B]\nalias = same\n", known)
    assert any("already the alias of" in p for p in twice), twice

    unusable = P.check_generated("[Alpha-7B]\nalias = two words\n", known)
    assert any("splits aliases on commas" in p for p in unusable), unusable


def test_apply_refuses_a_personality_whose_alias_collides(lib):
    """The collision check is on the write path, not only on the editor.

    `apply()` re-checks the text it is about to write, and a colliding alias is exactly the kind
    of value that could arrive from a stored config plus a library model added later.
    """
    _config("Beta-13B-shadow", "Alpha-7B", {"ctx-size": "1024"})
    P.save_personality("work", 1, "", ["Beta-13B-shadow"], {})
    text = P.render(P.find("work"), BUDGET)["text"]
    assert "alias = Beta-13B-shadow" in text
    assert P.check_generated(text, {r["id"] for r in mi.rows()}) == []

    forced = P.render({"name": "forced", "models_max": 1, "models": None,
                       "overrides": {}}, BUDGET)
    planted = forced["text"].replace("[*]", "[Alpha-7B]\nalias = Beta-13B\n\n[*]", 1)
    problems = P.check_generated(planted, {r["id"] for r in mi.rows()})
    assert any("refuses to start" in p for p in problems), problems


# ------------------------------------------------------------------- through the API

@pytest.fixture()
def client(lib):
    c = TestClient(app_main.create_app())
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_the_api_creates_a_config_and_a_personality_that_uses_it(client):
    r = client.post("/api/model-configs/save", json={
        "name": "fast-alpha", "base_model": "Alpha-7B", "note": "half the context",
        "settings": {"ctx-size": "2048", "parallel": "2"}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["settings"] == {"ctx-size": 2048, "parallel": 2}

    r = client.post("/api/personalities/save", json={
        "name": "work", "models_max": 1, "models": ["fast-alpha"], "global_edits": {}})
    assert r.status_code == 200, r.text
    assert r.json()["personality"]["configs"] == [{"name": "fast-alpha", "model": "Alpha-7B"}]

    r = client.get("/api/model-configs").json()
    assert [c["name"] for c in r["configs"]] == ["fast-alpha"]
    assert r["assigned"] == {"fast-alpha": ["work"]}

    r = client.post("/api/personalities/preview", json={"name": "work"})
    assert r.status_code == 200, r.text
    joined = "\n".join(r.json()["diff"])
    assert "alias = fast-alpha" in joined and "[Alpha-7B]" in joined
    assert r.json()["would_refuse"] == []


def test_the_page_lists_custom_personalities_and_model_configs(client):
    client.post("/api/model-configs/save", json={"name": "fast-alpha", "base_model": "Alpha-7B",
                                                 "settings": {"ctx-size": "2048"}})
    client.post("/api/personalities/save", json={"name": "work", "models_max": 1,
                                                 "models": ["fast-alpha"]})
    body = client.get("/personalities")
    assert body.status_code == 200
    text = body.text
    assert "fast-alpha" in text and "work" in text
    # The two built-ins are still offered, still labelled as not editable.
    assert "id=\"pers-safe\"" in text and "id=\"pers-default\"" in text


def test_the_api_refuses_what_the_store_refuses(client):
    """The status is asserted per case, not pinned to 400 for all of them. A refused *proposal* is a bad
    request; deleting a personality that was never saved is a thing that is not there, and calling
    that 400 would make "you typed the name wrong" and "that name is taken" the same answer.
    """
    cases = [
        ("/api/model-configs/save", {"name": "Alpha-7B", "base_model": "Alpha-7B"}, 400,
         "already a model in the library"),
        ("/api/model-configs/save", {"name": "ok", "base_model": "Alpha-7B",
                                     "settings": {"model": "/tmp/x.gguf"}}, 400,
         "router overwrites this key"),
        ("/api/model-configs/save", {"name": "ok2", "base_model": "NotAModel"}, 400,
         "no library entry"),
        ("/api/personalities/save", {"name": "safe", "models_max": 1}, 400,
         "built-in personality"),
        ("/api/personalities/delete", {"name": "default", "confirm": True}, 400,
         "built-in and cannot be deleted"),
        ("/api/personalities/delete", {"name": "never-saved", "confirm": True}, 404,
         "no personality named"),
    ]
    for url, payload, status, expected in cases:
        r = client.post(url, json=payload)
        assert r.status_code == status, (url, r.status_code, r.text)
        assert expected in r.json()["detail"], (url, r.json()["detail"])


def test_a_config_in_use_cannot_be_deleted_through_the_api(client):
    client.post("/api/model-configs/save", json={"name": "fast-alpha", "base_model": "Alpha-7B",
                                                 "settings": {"ctx-size": "2048"}})
    client.post("/api/personalities/save", json={"name": "work", "models_max": 1,
                                                 "models": ["fast-alpha"]})
    r = client.post("/api/model-configs/delete", json={"name": "fast-alpha", "confirm": True})
    assert r.status_code == 400 and "assigned by work" in r.json()["detail"], r.text
    r = client.post("/api/model-configs/delete", json={"name": "fast-alpha"})
    assert r.status_code == 400 and "confirm required" in r.json()["detail"]
    # Once nothing uses it, deleting it is allowed and the page stops offering it.
    client.post("/api/personalities/delete", json={"name": "work", "confirm": True})
    r = client.post("/api/model-configs/delete", json={"name": "fast-alpha", "confirm": True})
    assert r.status_code == 200, r.text
    assert client.get("/api/model-configs").json()["configs"] == []


def test_preview_still_refuses_per_model_edits_that_belong_to_a_config(client):
    """`model_edits` is still honoured — but it cannot address a model a config owns the name of.

    Two layers writing one key is the thing this design exists to avoid, and the API is where a
    hand-built payload could reintroduce it.
    """
    client.post("/api/model-configs/save", json={"name": "fast-alpha", "base_model": "Alpha-7B",
                                                 "settings": {"ctx-size": "2048"}})
    client.post("/api/personalities/save", json={"name": "work", "models_max": 1,
                                                 "models": ["fast-alpha"]})
    r = client.post("/api/personalities/preview", json={
        "name": "work", "model_edits": {"Alpha-7B": {"ctx-size": "3072"}}})
    assert r.status_code == 200, r.text
    body = r.json()
    added = [ln[1:].strip() for ln in body["diff"]
             if ln.startswith("+") and not ln.startswith("+++")]
    # Documented precedence: the override is the top layer, and the refusal to *store* such an
    # edit is a UI rule, not a generator rule. What has to hold here is that the outcome is the
    # documented one rather than whichever dict was applied last -- the file on offer carries 3072,
    # not the config's 2048. Read off the added lines rather than by reparsing the diff: a unified
    # diff carries no `[*]` header, and configparser is right to refuse that.
    assert "ctx-size = 3072" in added, added
    assert "ctx-size = 2048" not in added, added
    assert "alias = fast-alpha" in added, added
    assert body["would_refuse"] == [], body["would_refuse"]


# ------------------------------------------------------------- "active" is derived

def test_a_personality_is_active_exactly_when_the_file_on_disk_is_its_file(client, lib):
    """Nothing remembers which personality is running, so nothing can be wrong about it.

    And the corollary the page has to show: a hand-edited presets.ini matches none of them, which
    is reported as "nothing is active" rather than as a personality the router is not running.
    """
    client.post("/api/model-configs/save", json={"name": "fast-alpha", "base_model": "Alpha-7B",
                                                 "settings": {"ctx-size": "2048"}})
    client.post("/api/personalities/save", json={"name": "work", "models_max": 1,
                                                 "models": ["fast-alpha"]})

    def active_names():
        """Which cards the page marks active -- read from the marker's own attribute.

        The old helper found a card by slicing the page between `id="pers-` prefixes and searched
        for the badge string inside the slice. That is not a scope: a card's slice runs to the next
        id, so it swallows the next card's badge, and the deck's script text contains the badge as a
        selector. `data-active-for` names the card it belongs to, so the answer cannot be
        mis-attributed by how the page was cut (§6.14.1c Finding 1).
        """
        page = client.get("/personalities").text
        return set(re.findall(r'data-active-for="([^"]+)"', page))

    assert active_names() == set(), "the file on disk is a hand-written one"

    def live_pair():
        """(what the strips say is running, what the bodies say) -- read from both markers."""
        page = client.get("/personalities").text
        return (set(re.findall(r'data-live-strip="([^"]+)"', page)),
                set(re.findall(r'data-active-for="([^"]+)"', page)))

    text = P.render(P.find("work"), lib["budget"])["text"]
    paths.PRESETS_INI.write_text(text)
    assert active_names() == {"work"}
    # The deck states the same fact in two positions by design -- on the strip, so the deck
    # answers "which one is running" without opening anything, and in the raised body. Two
    # positions is exactly where the reverted deck attempt died (§6.14.1c Finding 2), so the two
    # are compared at the moment something IS running, not only once at the end.
    assert live_pair() == ({"work"}, {"work"}), "the deck and the card disagree about what is live"

    paths.PRESETS_INI.write_text(ORIGINAL + "\n# edited by hand\n")
    assert active_names() == set()
    assert live_pair() == (set(), set()), "nothing is running, and something claims it is"


def test_live_state_is_stated_once_per_card_and_only_by_attribute(client, lib):
    """The marker names its own card, and no marker exists without that name.

    Two reasons this is a test and not a style preference. The deck reads live state on the strip
    and under the fold, so the value must have one source; and the guard that checks it must not
    depend on where in the page the marker sits. `data-active-for` answers both: one attribute per
    active card, and the visible badge cannot be present without it.
    """
    page = client.get("/personalities").text
    marks = re.findall(r'data-active-for="([^"]+)"', page)
    assert len(marks) == len(set(marks)), f"a card states live state more than once: {marks}"
    badges = page.count('class="badge yes pers-active"')
    assert badges == len(marks), (
        f"{badges} live badges, {len(marks)} attributed: a live badge exists that does not name "
        "the card it belongs to, which is the sentence an operator acts on")

    # The deck states the same fact twice by design -- on the strip (answered without opening
    # anything) and in the body (the raised card's own account). Two positions is exactly where the
    # reverted deck attempt died, so the two sets are compared rather than trusted.
    strip = set(re.findall(r'data-live-strip="([^"]+)"', page))
    assert strip == set(marks), (
        f"the strip says {sorted(strip)}, the body says {sorted(marks)}: the deck and the card "
        "disagree about what is running")
