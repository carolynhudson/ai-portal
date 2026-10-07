"""The settings registry as renderer data: one row per key, one shape for every control.

Why this exists
---------------
`personality.SETTINGS` is the validator. It has always carried `kind`, `section`, `default`,
`choices` and `accepts`, and the personalities page reads it directly -- which is why that page
is 16,720 px tall: every key is rendered with a paragraph of help inline, in three places, with
the control shape worked out in the template by `{% if s.choices or s.kind == 'bool' %}`.

docs/PORTAL_PLAN.md §6.14.1 asks for the opposite arrangement: the registry row grows the fields
a *renderer* needs, the page renders one row type from that data, and llama.cpp growing flags is
a data edit rather than a page edit. This module is that row structure. It adds nothing about
what a key means -- every field here is derived from `SETTINGS` (the meaning), from the model
index (the facts a row's visibility depends on), or from the build's own stated defaults.

What each field is for
----------------------
    kind            the renderer's control, not llama.cpp's parameter type
    scope           where the key may be set: global section, model section, or both
    aliases         the spellings this build declares (`-ngl`, `--gpu-layers`), measured
    default         the portal's value for "no opinion", what `render()` omits
    server_default  what the build says its default is, when it differs
    specials        words a numeric field accepts that are not numbers (`auto`, `all`)
    range/values    the bounds or the allowed list, where the build documents them
    visible_when    model-index facts that must be true for this row to mean anything
    substitutions   measured facts the help text quotes, resolved by `substitutions()`

Failure implication
-------------------
An unknown `visible_when` fact raises rather than evaluating false. A visibility rule that
quietly evaluates false hides a setting the operator can no longer reach, and "the editor does
not show it" is indistinguishable from "this build does not support it" -- the same shape as the
fallback that was the implementation (§6.13) and the probe that reported a clean audit after
measuring an embedder (§6.12.25). An unresolvable `{substitution}` raises for the same reason: a
help line that renders `Each costs {checkpoint_mib}` is a hole in the page, not a string.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from app.core import kb
from app.core import personality as P

#: The control a row renders. Deliberately smaller than llama.cpp's parameter vocabulary: the
#: page needs a widget per row, and `choice-or-int` is a number field with words in it.
RENDERER_KINDS = ("switch", "number", "text", "choice")

_KIND_TO_RENDERER = {
    "bool": "switch",
    "int": "number",
    "float": "number",
    "choice": "choice",
    "choice-or-int": "number",
    "string": "text",
    "path": "text",
    "list": "text",
}

#: Scopes a row can declare, aligned with the preset sections rather than the registry's wording.
SCOPES = ("global", "model", "both")
_SECTION_TO_SCOPE = {"both": "both", "model": "model", "global": "global"}

#: The facts a visibility rule may name, each one a column the model index actually stores
#: (§4.9 Phase N) and each reading that column the way `modelindex.capabilities()` reads it.
#:
#: A rule naming a fact not in this table raises. Inventing `reasoning` here, for example, would
#: be an assertion that the index reads chat-template reasoning fields, which it does not: the
#: column does not exist, so the honest answer for `reasoning-effort` is "always show, no fact".
VISIBLE_FACTS: dict[str, Callable[[dict], bool]] = {
    # The tokenizer carries FIM specials, so `/infill` is a thing this file can do. Read as
    # truthiness, never `is True`: the column is a SQLite boolean, which comes back 1.
    "fim": lambda r: bool(r.get("has_fim")),
    # The file carries its own MTP/nextn head, so a draft spec can run without a second model.
    "nextn": lambda r: (r.get("nextn_layers") or 0) > 0,
    # A projector exists, so image input is addressable. `has_vision` is the header's claim;
    # the mmproj sidecar is what makes it usable, and `has_mmproj` is the index's own column.
    "vision": lambda r: bool(r.get("has_vision")),
    "mmproj": lambda r: bool(r.get("has_mmproj")),
    # pooling_type >= 4 is the ranking pool: /rerank answers. Below that it 501s (§4.9.3).
    "ranking": lambda r: (r.get("pooling_type") or 0) >= 4,
    # A chat template exists, so a template-level switch has something to act on.
    "chat_template": lambda r: bool(r.get("has_chat_template")),
    # The chat template emits tool protocol literally, so tool-calling is trained-in.
    "tools": lambda r: "tools" in (r.get("template_markers") or ""),
    # Sliding-window attention: `full_attention_interval` > 1 is the header saying only some
    # layers keep full context. Context checkpoints are gated per model on this, so the
    # checkpoint rows are meaningless on a model that reports 1.
    "sliding_window": lambda r: (r.get("full_attention_interval") or 1) > 1,
    "moe": lambda r: bool(r.get("is_moe")),
    "ssm_state": lambda r: bool(r.get("has_ssm_state")),
}

#: Numbers the help text quotes, and where each comes from. A row declares the names it uses;
#: `substitutions()` resolves them for the page, the bench tool and the probe from one place,
#: so a help line and a budget can quote different numbers for the same quantity.
SUBSTITUTION_FACTS = ("cache_ram_mib", "checkpoint_mib", "threads_default", "pool_gib",
                      "ctx_trained", "spec_types")

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")


class SettingRowError(Exception):
    """The row data asks for something the registry, the index, or the build cannot answer."""


# ------------------------------------------------------------------ one row
def renderer_kind(spec: dict) -> str:
    """The control this key's parameter type needs.

    Derived from the registry's `kind`, so a kind the row layer does not know raises instead of
    falling back to a text box: a number field rendered as free text is a validator that never runs.
    """
    kind = (spec or {}).get("kind")
    if kind not in _KIND_TO_RENDERER:
        raise SettingRowError(f"no renderer kind for parameter kind {kind!r}")
    return _KIND_TO_RENDERER[kind]


def scope_of(spec: dict) -> str:
    """Where the key may be set, in preset-section terms."""
    section = (spec or {}).get("section")
    if section not in _SECTION_TO_SCOPE:
        raise SettingRowError(f"no scope for section {section!r}")
    return _SECTION_TO_SCOPE[section]


def visible_when_of(key: str, spec: dict) -> list[str]:
    """The model-index facts this row needs before it means anything, validated against the table.

    Unknown names raise. A visibility rule that evaluates false on a fact nobody measured is the
    setting disappearing from the page with no explanation, which is the failure this module's
    docstring names.
    """
    facts = list((spec or {}).get("visible_when") or [])
    unknown = [f for f in facts if f not in VISIBLE_FACTS]
    if unknown:
        raise SettingRowError(
            f"{key}: visible_when names {unknown}, which the model index cannot answer; "
            f"known facts are {sorted(VISIBLE_FACTS)}")
    return facts


def rule_of(key: str, spec: dict) -> str:
    """One line saying what a legal value looks like, derived from the row's own data.

    Derived rather than written so it cannot disagree with `accepts()`: the editor shows this next
    to the control, and the generator enforces `accepts`. A hand-written rule is a second, weaker
    statement about the same key -- the exact shape §6.14.1 asks to delete.
    """
    kind = (spec or {}).get("kind")
    choices = list((spec or {}).get("choices") or [])
    values = list((spec or {}).get("values") or [])
    bounds = (spec or {}).get("range")
    specials = list((spec or {}).get("specials") or [])
    parts: list[str] = []
    if kind == "list":
        # A list field is not "one of": the value is a comma-separated selection, so the rule has
        # to say which items may appear in it. Saying "one of" would describe a different field.
        parts.append("comma-separated of: " + ", ".join(str(v) for v in values))
    elif choices:
        parts.append("one of: " + ", ".join(str(c) for c in choices))
    if specials:
        parts.append("or " + ", ".join(str(s) for s in specials))
    if bounds:
        lo, hi = bounds
        parts.append(f"{lo} .. {hi}" if hi is not None else f"{lo} or more")
    if not parts:
        parts.append(str(kind))
    return "; ".join(parts)


def row(key: str) -> dict:
    """One settings row, in the shape the row component renders.

    Raises for a key the registry does not define. The page is allowed to render a key the router
    strips (`writable` false) only in the keys table, which says so on the row; the editor's
    controls come from `editable_rows()`.
    """
    spec = P.SETTINGS.get(key)
    if spec is None:
        raise SettingRowError(f"no setting definition for {key!r}")
    cite = str(spec.get("cite") or "")
    return {
        "key": key,
        "aliases": list(spec.get("aliases") or ()),
        "kind": renderer_kind(spec),
        "param_kind": spec.get("kind"),
        "scope": scope_of(spec),
        "default": spec.get("default"),
        "server_default": spec.get("server_default", P.SERVER_DEFAULTS.get(key)),
        "choices": list(spec.get("choices") or ()),
        "values": list(spec.get("values") or ()),
        "specials": list(spec.get("specials") or ()),
        "range": spec.get("range"),
        "unit": spec.get("unit"),
        "rule": rule_of(key, spec),
        "help": spec.get("help"),
        "detail": spec.get("side_effects"),
        "cite": cite,
        "help_ref": kb.ref_of(cite) if cite else None,
        "visible_when": visible_when_of(key, spec),
        "substitutions": list(spec.get("substitutions") or ()),
        "preset_only": bool(spec.get("preset_only")),
        "preset_mapped": bool(spec.get("preset_mapped")),
        "writable": P.setting_is_writable(key),
        "omitted_when_equal_to": P.SERVER_DEFAULTS.get(key),
    }


def rows(scope: str | None = None) -> list[dict]:
    """Every registry row, in declaration order, optionally narrowed to one scope.

    `scope="global"` means "may be set in `[*]`", which includes `both`; the generator's own
    refusal of model-scoped keys in `[*]` (personality.render) is the rule, not this filter.
    """
    out = [row(k) for k in P.setting_names()]
    if scope is None:
        return out
    if scope not in SCOPES:
        raise SettingRowError(f"no such scope {scope!r}; known: {list(SCOPES)}")
    if scope == "global":
        return [r for r in out if r["scope"] in ("global", "both")]
    if scope == "model":
        return [r for r in out if r["scope"] in ("model", "both")]
    return out


def editable_rows(scope: str | None = None) -> list[dict]:
    """Rows the editor may offer controls for -- keys the portal can actually emit.

    The keys table shows the router-controlled keys with a refusal on the row; the editor must not
    render a control for a key the router overwrites at launch, because a control that changes
    nothing is what the operator will then rely on.
    """
    return [r for r in rows(scope) if r["writable"]]


# ------------------------------------------------------------------ visibility
def visible(r: dict, model_row: dict | None) -> bool:
    """Does this row mean anything for this model?

    A row with no `visible_when` is always visible. A row with facts needs them all true. With no
    model row at all (the global section, which names no model), every row is visible: nothing has
    claimed the model lacks a fact.
    """
    facts = r.get("visible_when") or ()
    if not facts or model_row is None:
        return True
    return all(VISIBLE_FACTS[f](model_row) for f in facts)


def rows_for_model(model_row: dict | None, scope: str | None = "model") -> list[dict]:
    """Rows offerable for one model, filtered by what that model's header says.

    This is §6.14.1's "visibility is measured, not asserted": the checkpoint rows disappear for a
    model that reports no sliding window, the spec rows for a file with no draft head, the jinja
    row for a file with no chat template. It reads the index columns, so the answer changes with
    the library instead of with a hardcoded list in a template.
    """
    return [r for r in rows(scope) if visible(r, model_row)]


def hidden_reason(r: dict, model_row: dict | None) -> str | None:
    """Why a row is not offerable for this model, naming the fact the header did not provide.

    The page needs this: a setting that silently vanishes reads as a build that lacks it. Naming
    the missing fact is what makes the absence an answer.
    """
    if model_row is None:
        return None
    missing = [f for f in (r.get("visible_when") or ()) if not VISIBLE_FACTS[f](model_row)]
    if not missing:
        return None
    return "needs " + " + ".join(missing) + " in the model's header"


# ------------------------------------------------------------------ substitutions
def substitutions(model_row: dict | None = None, pool_bytes: int | None = None) -> dict[str, str]:
    """The measured facts a help line may quote, resolved from the box and the index.

    Each entry is a fact the portal already measures elsewhere; the point of resolving them here
    is that a help line and a budget cannot then disagree about the same number.
    """
    out: dict[str, str] = {
        "cache_ram_mib": str(P.SERVER_DEFAULTS.get("cache-ram", "unknown")),
        "checkpoint_mib": "19-50 MiB",                                # §6.12.6, measured per model
        "threads_default": str(P.GLOBAL_DEFAULTS.get("threads", "unset")),
        "spec_types": ", ".join(str(c) for c in P.SETTINGS.get("spec-type", {}).get("choices") or ()),
    }
    if pool_bytes:
        out["pool_gib"] = f"{pool_bytes / (1 << 30):.1f}"
    if model_row:
        trained = model_row.get("ctx_trained")
        if trained:
            out["ctx_trained"] = str(int(trained))
    return out


def expand(text: str, facts: dict[str, str]) -> str:
    """Substitute `{name}` in help text, refusing a name nothing can answer.

    A placeholder left unsubstituted is a hole in a rendered page, and a page full of `{...}`
    reads as a templating bug rather than as a fact the portal never had.
    """
    wanted = {m.group(1) for m in _PLACEHOLDER.finditer(text or "")}
    missing = sorted(wanted - set(facts))
    if missing:
        raise SettingRowError(
            f"help text quotes {missing}, which nothing resolves; known: {sorted(SUBSTITUTION_FACTS)}")
    return _PLACEHOLDER.sub(lambda m: facts[m.group(1)], text or "")


def substitution_names(r: dict) -> list[str]:
    """The facts a row's help and detail lines quote, read from the text itself.

    Declared in the registry *and* derivable from the prose: comparing the two is what catches a
    help line that gained a placeholder the row never declared.
    """
    declared = set(r.get("substitutions") or ())
    in_text = {m.group(1) for m in _PLACEHOLDER.finditer(
        (r.get("help") or "") + " " + (r.get("detail") or ""))}
    return sorted(declared | in_text)
