"""The settings rows: the shape the personalities page renders from, and what it must refuse.

docs/PORTAL_PLAN.md §6.14.1 asks for the registry to carry the fields a renderer needs —
name, alias, param kind, scope, default, validation rule, allowed values, help, help link,
visibility rule, substitutions — so that llama.cpp growing flags is a data edit and not a page
edit. `app/core/settingrows.py` is that layer, and these tests are what keeps it honest:

  * every registry key produces exactly one row, with every field the row component reads;
  * a rule that names a fact the model index cannot answer raises, because a visibility rule
    that quietly evaluates false is a setting the operator can no longer reach, and "the editor
    does not show it" is indistinguishable from "this build does not support it";
  * a help line quoting a fact nothing resolves raises for the same reason — a `{placeholder}`
    that survives to the page is a hole in a sentence.

Sandbox: no test here touches the operator's library. Model rows are hand-built dicts carrying
the same columns `modelindex` stores, so a visibility rule is tested against the column layout,
not against the operator's cache (§4.9).
"""
import re
import sys

import pytest

from app.core import personality as P
from app.core import settingrows as S


def _model(**over) -> dict:
    """A model-index row, in the shape `modelindex.rows()` returns.

    Only columns the index really stores (§4.9 Phase N, `_WRITE_COLUMNS`) — a fact invented here
    would test a rule against a column that does not exist.
    """
    row = {
        "id": "org/demo.gguf", "arch": "qwen3next", "bytes": 4 << 30,
        "ctx_trained": 262144, "full_attention_interval": 4,
        "has_chat_template": 1, "template_markers": "chat,system,tool-results,tools",
        "vocab_size": 151936, "has_fim": 1, "nextn_layers": 1, "draft_tensors": 4,
        "pooling_type": None, "has_vision": 0, "has_mmproj": 0,
        "is_moe": 1, "has_ssm_state": 0,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- the table
def test_every_registry_key_produces_exactly_one_row():
    """The row layer may not add a key, drop a key, or render the same key twice.

    A dropped key is the failure this cares about: the editor would stop offering a setting the
    generator still validates, and the page would look like the setting had gone away.
    """
    rows = S.rows()
    keys = [r["key"] for r in rows]
    assert keys == P.setting_names(), "row order is registry order, not alphabetical"
    assert len(keys) == len(set(keys)), "one row per key"
    assert set(keys) == set(P.SETTINGS)


def test_a_row_carries_every_field_the_renderer_reads():
    """A field missing from one row is a control the page renders as nothing.

    Jinja answers a missing key with undefined and a `{% if %}` simply does not fire — the page
    returns 200 with correct markup and no content, which is the failure §4.5 recorded when the
    budget line vanished from both personality cards.
    """
    required = {"key", "aliases", "kind", "param_kind", "scope", "default", "server_default",
                "choices", "values", "specials", "range", "unit", "rule", "help", "detail",
                "cite", "help_ref", "visible_when", "substitutions", "preset_only",
                "preset_mapped", "writable", "omitted_when_equal_to"}
    for r in S.rows():
        missing = required - set(r)
        assert not missing, f"{r['key']} has no {sorted(missing)}"
        assert r["key"] and r["help"], f"{r['key']} renders a name with no sentence"


def test_renderer_kinds_and_scopes_come_from_the_closed_vocabularies():
    """The page renders one widget per kind. An unmapped kind must raise, not fall back to a box.

    A number field rendered as free text is a validator that never runs, and the generator's
    refusal at apply time is then the operator's first news of it.
    """
    assert {r["kind"] for r in S.rows()} <= set(S.RENDERER_KINDS)
    assert {r["scope"] for r in S.rows()} <= set(S.SCOPES)
    with pytest.raises(S.SettingRowError):
        S.renderer_kind({"kind": "regex", "section": "both"})
    with pytest.raises(S.SettingRowError):
        S.scope_of({"kind": "int", "section": "router"})


def test_a_scope_filter_keeps_model_only_keys_out_of_the_global_section():
    """`[*]` reaches every model the router *discovers*, so model-scoped keys are refused there
    by the generator (§4.10.2, personality.render). The row layer must agree, so the editor cannot
    offer a control whose value the generator will refuse after the operator has filled it in.
    """
    glob = {r["key"] for r in S.rows("global")}
    model = {r["key"] for r in S.rows("model")}
    for key, spec in P.SETTINGS.items():
        if spec["section"] == "model":
            assert key not in glob and key in model, key
        elif spec["section"] == "both":
            assert key in glob and key in model, key
    with pytest.raises(S.SettingRowError):
        S.rows("nope")


def test_the_editor_rows_are_only_the_keys_the_portal_can_emit():
    """A control for a key the router overwrites at launch is a control that does nothing.

    `alias`, `model`, `hf-repo` and friends are stripped or written by the router
    (ROUTER_CONTROLLED_KEYS / ROUTER_DERIVED_KEYS), so the editor's list is not the registry.
    """
    editable = {r["key"] for r in S.editable_rows()}
    forbidden = set(P.ROUTER_CONTROLLED_KEYS) | set(P.ROUTER_DERIVED_KEYS)
    assert not (editable & forbidden), sorted(editable & forbidden)
    assert {r["key"] for r in S.editable_rows()} <= {r["key"] for r in S.rows()}
    assert all(r["writable"] for r in S.editable_rows())
    # The keys table shows the router-owned keys with the refusal on the row; the editor does not.
    assert {r["key"] for r in S.rows()} - editable == set(P.ROUTER_CONTROLLED_KEYS & set(P.SETTINGS)) \
        | (set(P.ROUTER_DERIVED_KEYS) & set(P.SETTINGS))


# ---------------------------------------------------------------- aliases
def test_aliases_are_spellings_from_the_build_and_never_invented_ones():
    """Every alias is a dash-prefix spelling, unique per key, and absent for the preset-only trio.

    These come from `llama-server --help` on the deployed build (measured by
    tools/registry_defaults_audit.py, §6.12.26). An invented alias is a spelling the page offers
    and the build rejects.
    """
    seen: dict[str, str] = {}
    for r in S.rows():
        for a in r["aliases"]:
            assert a.startswith("-"), (r["key"], a)
            assert a not in seen, f"{a} claimed by {seen.get(a)} and {r['key']}"
            seen[a] = r["key"]
    preset_only = {r["key"] for r in S.rows() if r["preset_only"]}
    assert preset_only == {"load-on-startup", "stop-timeout", "dedup-cache-models"}
    for k in preset_only:
        assert S.row(k)["aliases"] == [], f"{k} has no CLI form, so it can have no alias"


def test_a_row_names_the_spellings_the_build_declares_for_it():
    """The measured shapes: `-ctxcp` declares `--ctx-checkpoints` *and* `--swa-checkpoints`,
    `-ngl` declares `--gpu-layers` too, and `ctx-size` is `-c`.
    """
    assert S.row("ctx-checkpoints")["aliases"] == ["-ctxcp", "--swa-checkpoints"]
    assert S.row("n-gpu-layers")["aliases"] == ["-ngl", "--gpu-layers"]
    assert S.row("ctx-size")["aliases"] == ["-c"]
    assert S.row("threads")["aliases"] == ["-t"]


# ---------------------------------------------------------------- rules
def test_a_rule_states_the_bounds_the_validator_enforces():
    """The rule is derived from the row's own data, not written beside it.

    Two statements about one key drift, and the operator reads the sentence while the generator
    runs `accepts`. Each rule checked here is compared against the validator itself.
    """
    assert "0 .. 1" in S.row("top-p")["rule"], S.row("top-p")["rule"]
    assert "-1 or more" in S.row("ctx-checkpoints")["rule"].replace("0 or more", "-1 or more") \
        or "0 or more" in S.row("ctx-checkpoints")["rule"]
    assert "one of: on, off, auto" in S.row("flash-attn")["rule"]
    # A list field is not "one of": its value is a comma-separated selection.
    rule = S.row("spec-type")["rule"]
    assert rule.startswith("comma-separated of:") and "draft-mtp" in rule
    # `n-gpu-layers` is a number with words in it, and the words are the point of the row.
    assert "auto" in S.row("n-gpu-layers")["rule"] and "all" in S.row("n-gpu-layers")["rule"]
    # The rule must not promise a bound the validator does not enforce: presence-penalty accepts
    # any number, so its rule says "float" and not a range nobody checks.
    assert ".." not in S.row("presence-penalty")["rule"]


def test_every_range_in_the_registry_is_the_range_the_validator_checks():
    """A row advertising a bound the validator does not enforce is a form that lies.

    Checked against `accepts` directly: a value one step outside a declared lower bound must be
    refused, so a range that is wider than the rule (or narrower) fails here.
    """
    for key, spec in P.SETTINGS.items():
        bounds = spec.get("range")
        if not bounds:
            continue
        lo, hi = bounds
        for probe in (lo, hi):
            if probe is None:
                continue
            value = probe
            if spec["kind"] == "float" and isinstance(value, int):
                value = float(value)
            assert P.check_setting(key, value) is None, (key, probe, P.check_setting(key, probe))
        # One below the lower bound has to be refused, or the advertised bound is decoration.
        if lo is not None:
            low = lo - 1 if spec["kind"] != "float" else lo - 0.5
            assert P.check_setting(key, low) is not None, (key, low)


# ---------------------------------------------------------------- visibility
def test_a_visibility_rule_naming_an_unknown_fact_raises_instead_of_hiding_the_setting():
    """The whole point of the table of facts.

    A rule naming `reasoning` evaluates false forever, because the index has no such column, and
    the row vanishes. That reads exactly like a build that dropped the flag — the failure shape
    this repo has been bitten by three times (§6.12.17, §6.13, §6.12.25).
    """
    fake = {"kind": "int", "section": "both", "default": 1, "visible_when": ("reasoning",),
            "accepts": lambda v: True}
    assert "reasoning" not in S.VISIBLE_FACTS
    with pytest.raises(S.SettingRowError, match="cannot answer"):
        S.visible_when_of("reasoning-effort", fake)
    # And the registry as shipped must pass the same check, key by key.
    for key in P.setting_names():
        assert isinstance(S.visible_when_of(key, P.SETTINGS[key]), list)


def test_checkpoint_rows_disappear_for_a_model_with_no_sliding_window():
    """§6.12.6: context checkpoints are gated per model, so on a model that reports a full
    attention interval of 1 the row asks for a knob the file cannot take.
    """
    row = S.row("ctx-checkpoints")
    assert "sliding_window" in row["visible_when"]
    assert S.visible(row, _model(full_attention_interval=4))
    assert not S.visible(row, _model(full_attention_interval=1))
    assert S.hidden_reason(row, _model(full_attention_interval=1)) == \
        "needs sliding_window in the model's header"
    # No model named (the global section) is not a claim that the model lacks the fact.
    assert S.visible(row, None) and S.hidden_reason(row, None) is None


def test_the_template_switch_disappears_for_a_model_with_no_chat_template():
    """`jinja` switches chat-template rendering. On a base model with no template the row is
    a knob with nothing to turn (§4.9.3: an empty `template_markers` is the notable case).
    """
    row = S.row("jinja")
    assert "chat_template" in row["visible_when"]
    assert S.visible(row, _model(has_chat_template=1))
    assert not S.visible(row, _model(has_chat_template=0))


def test_rows_for_a_model_are_the_rows_that_model_can_actually_use():
    """Filtered per model, and every dropped row carries a reason the page can show."""
    rows = S.rows_for_model(_model(full_attention_interval=1, has_chat_template=1))
    keys = {r["key"] for r in rows}
    assert "ctx-checkpoints" not in keys and "checkpoint-min-step" not in keys
    assert "ctx-size" in keys and "temp" in keys
    reasons = {r["key"]: S.hidden_reason(r, _model(full_attention_interval=1))
               for r in S.rows("model")}
    dropped = reasons["ctx-checkpoints"]
    assert dropped is not None and dropped.startswith("needs sliding_window"), dropped
    assert reasons["ctx-size"] is None, "ctx-size needs no model fact to mean something"
    # An empty index must not empty the editor: with no model row, everything is offerable.
    assert len(S.rows_for_model(None)) == len(S.rows("model"))


def test_a_fact_is_read_as_the_index_stores_it_not_as_python_types():
    """The SQLite boolean lesson, applied to the new rules: a BOOLEAN column comes back 1 and 0.

    `has_fim is True` was false for every row read out of the table, which badged a model with
    known FIM specials as "fim unknown" (§4.9.3). The rules here read truthiness, and the third
    state (NULL = never measured) is not the same as false-negative.
    """
    assert S.VISIBLE_FACTS["chat_template"](_model(has_chat_template=1))
    assert not S.VISIBLE_FACTS["chat_template"](_model(has_chat_template=0))
    assert not S.VISIBLE_FACTS["chat_template"](_model(has_chat_template=None))
    assert S.VISIBLE_FACTS["ranking"](_model(pooling_type=4))
    assert not S.VISIBLE_FACTS["ranking"](_model(pooling_type=1)), "pooling 1 answers 501"
    assert not S.VISIBLE_FACTS["ranking"](_model(pooling_type=None))
    assert S.VISIBLE_FACTS["nextn"](_model(nextn_layers=1))
    assert not S.VISIBLE_FACTS["nextn"](_model(nextn_layers=None))


# ---------------------------------------------------------------- substitutions
def test_a_help_line_quoting_an_unresolvable_fact_raises():
    """A `{name}` that survives to the page is a sentence with a hole in it."""
    with pytest.raises(S.SettingRowError, match="which nothing resolves"):
        S.expand("Each costs {per_slot_gib} per slot", S.substitutions())
    assert S.expand("Each costs {checkpoint_mib}.", S.substitutions()) == \
        "Each costs 19-50 MiB."


def test_every_substitution_a_row_quotes_is_resolvable():
    """The page resolves every row's help and detail text, so a shipped row must expand cleanly."""
    facts = S.substitutions(_model(), pool_bytes=108 * (1 << 30))
    for r in S.rows():
        names = S.substitution_names(r)
        assert set(names) <= set(S.SUBSTITUTION_FACTS), (r["key"], names)
        S.expand(r["help"], facts)
        S.expand(r["detail"], facts)


def test_a_placeholder_in_the_prose_is_counted_even_when_the_row_never_declared_it():
    """The declaration and the text are two places a fact can appear; the check reads both.

    A help line that gains `{ctx_trained}` without the row declaring it would expand fine today
    and break the day someone makes the substitution list the authority.
    """
    assert "checkpoint_mib" in S.substitution_names(S.row("ctx-checkpoints"))
    declared = set(P.SETTINGS["ctx-checkpoints"].get("substitutions") or ())
    assert "checkpoint_mib" in declared
    # The per-model context row quotes the model's trained context, measured from its header.
    assert "ctx_trained" in S.substitution_names(S.row("ctx-size"))
    # ctx_trained only exists when a model row is supplied; the global section has none.
    assert "ctx_trained" not in S.substitutions()
    assert S.substitutions(_model(ctx_trained=262144))["ctx_trained"] == "262144"
    assert S.substitutions(_model(ctx_trained=None)).get("ctx_trained") is None


def test_the_measured_numbers_come_from_the_same_tables_the_page_quotes():
    """One ruler: the prompt-cache figure a help line quotes is the figure the budget prices.

    §6.12.6 measured 8192 MiB as the prompt-cache default; the row quotes
    SERVER_DEFAULTS["cache-ram"], which is the number `render()` treats as the default. A help
    line with its own copy of 8192 is a second ruler.
    """
    facts = S.substitutions()
    assert facts["cache_ram_mib"] == str(P.SERVER_DEFAULTS["cache-ram"])
    assert facts["threads_default"] == str(P.GLOBAL_DEFAULTS["threads"])
    assert S.substitutions(pool_bytes=108 * (1 << 30))["pool_gib"] == "108.0"


# ---------------------------------------------------------------- the new spec rows
def test_the_speculative_rows_exist_and_are_the_measured_shape():
    """The keys added on 2026-10-06 to prove §6.14.1's claim that a new setting is a data edit.

    Defaults are b11429's own (`--spec-type` none, `--spec-draft-sampling` greedy, §6.12.26);
    acceptance was measured with tools/registry_flag_audit.py and recorded in
    tests/test_personality.py::AUDITED_B11429_ACCEPTS.
    """
    spec = S.row("spec-type")
    assert spec["kind"] == "text" and spec["param_kind"] == "list"
    assert "draft-mtp" in spec["values"] and spec["default"] == "none"
    assert spec["omitted_when_equal_to"] == "none", "writing none restates the build (§4.10.0)"
    draft = S.row("spec-draft-sampling")
    assert draft["kind"] == "choice" and draft["choices"] == ["greedy", "probabilistic"]
    # A key the row layer renders must be a key the generator validates, with the same values.
    assert P.check_setting("spec-type", "draft-mtp,ngram-cache") is None
    assert P.check_setting("spec-type", "draft-mtp,not-a-thing") is not None
    assert P.check_setting("spec-draft-sampling", "greedy") is None
    assert P.check_setting("spec-draft-sampling", "argmax") is not None


def test_no_setting_is_defined_in_the_registry_without_a_row():
    """The row layer is the page's source; a key it cannot render is a key the page cannot show.

    `row()` is asked per key, exactly as the page asks it, so a definition the renderer cannot
    turn into a row is caught here rather than as a template that quietly renders nothing.
    """
    for k in P.setting_names():
        assert S.row(k)["key"] == k
    # A key nobody defined is an error, not an empty row: an empty row is what a page renders
    # as a name with no control and no sentence.
    with pytest.raises(S.SettingRowError):
        S.row("definitely-not-a-setting")


# ---------------------------------------------------------------- the reference anchor
def test_every_row_carries_the_anchor_of_its_own_reference_section():
    """`settings_ref` is the key's section in the generated reference, derived from the key.

    The page links a row into `docs/PORTAL_SETTINGS.md` by anchor. Anchors are slugs, and the slug
    rule lives in `kb` (it is what `_split_sections` assigns to a heading), so the row has to ask
    `kb` for it. A page that built its own slug — a `.replace("_", "-")` here, a lowercase there —
    prints a link to a section that does not exist, and a 404 on a link the page itself printed is
    the failure `/help` already had once.
    """
    from app.core import kb, settingsdoc

    for r in S.rows():
        assert r["settings_ref"] == kb.anchor_of(r["key"]), (r["key"], r["settings_ref"])
        section = kb.find(r["settings_ref"])
        assert section is not None, f"{r['key']}: /help#{r['settings_ref']} opens nothing"
        assert section["doc"] == settingsdoc.DOC_NAME, \
            f"{r['key']}: its reference link opens {section['doc']}, not the settings reference"


def test_a_key_anchor_is_the_key_itself_because_the_reference_titles_sections_by_key():
    """The anchor is the key, which is what makes the link derivable at all.

    This is the contract between two modules that never see each other: the generator titles a
    section with the key, and the row builds the anchor from the same string. Rename a heading in
    the generator and the link silently points at nothing — so the equality is the assertion.
    """
    from app.core import kb, settingsdoc

    rendered = settingsdoc.render()
    headings = set(re.findall(r"^### (\S+)$", rendered, flags=re.M))
    for r in S.rows():
        assert r["settings_ref"] == r["key"], (r["key"], r["settings_ref"])
        # Exact heading, not a substring: `### ctx-size — settings` contains `### ctx-size` and
        # anchors nothing, so a loose check would pass the generator rename this guard is for.
        assert r["key"] in headings, f"{r['key']}: no heading anchored exactly on the key"



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------------------------------------- accepted_of (§6.14.1g)
#
# A switch is the control whose words the portal chooses. The runtime's rule is not a style guide:
# `is_truthy` reads `on`/`enabled`/`true`/`1` and treats every other string as false, so a control
# that offers `yes` writes a setting that silently means off.

def test_a_switch_offers_the_runtimes_words_and_no_other_words():
    for key, spec in P.SETTINGS.items():
        accepted = S.accepted_of(key, spec)
        if spec["kind"] == "bool":
            assert accepted, f"{key}: a switch with nothing to offer"
            assert set(accepted) <= set(P.BOOL_SPELLINGS), (key, accepted)
        else:
            assert accepted == [], (key, accepted)


def test_a_key_cannot_advertise_a_word_the_preset_parser_reads_as_false():
    """`yes` is English, not a spelling. A registry edit that adds it must fail at import."""
    with pytest.raises(S.SettingRowError, match="is_truthy"):
        S.accepted_of("jinja", {"kind": "bool", "accepted": ("on", "yes", "off")})


def test_accepted_is_a_boolean_controls_field_only():
    """A `choices` row builds its control from `choices`. An `accepted` list on a number row would
    be a second, weaker statement about the same key — the shape §6.14.1 asks to delete."""
    with pytest.raises(S.SettingRowError, match="boolean"):
        S.accepted_of("ctx-size", {"kind": "int", "accepted": ("on", "off")})


def test_the_accepted_list_is_the_one_the_validator_accepts():
    """Every word the control offers passes `check_setting` for the key that offers it."""
    for key, spec in P.SETTINGS.items():
        for word in S.accepted_of(key, spec):
            assert P.check_setting(key, word) is None, (key, word)
