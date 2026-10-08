"""The settings reference: the settings registry, rendered as a knowledgebase document.

Why this file exists. The personalities page used to carry a table of every key a personality
can set — key, type, scope, default, what it does, the section that proves it — right below the
editor, whose rows say the same thing. Two renderings of one registry on one page is
duplication, and duplication is how pages drift. The operator's note asked for the content where
the content belongs: a detailed report in the knowledgebase, alongside the prose it cites.

Single source. Everything here is read from `app.core.settingrows` (registry data, plan §6.14.1).
Nothing in this module decides what a key means: it formats facts the registry already carries.
A new key is a registry edit, and `--write` picks it up; `tests/test_settingsdoc.py` fails if the
committed document no longer equals what `render()` produces, so a registry change that skips
regeneration is a test failure rather than a stale page.

Anchors. Each key gets a heading of its own, which `app.core.kb` turns into `/help#<key>`: the
editor rows link into the reference by key, and a key with no section is a missing row in the
registry, not a broken anchor.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from . import settingrows

DOC_NAME = "PORTAL_SETTINGS.md"
DOC_TITLE = "Settings reference — every key a personality can set, generated from the registry"

_SCOPE_WORDS = {
    "global": "global only",
    "model": "model only",
    "both": "global and per-model",
}

_GENERATED_BANNER = """<!-- GENERATED FILE — do not edit by hand. -->
<!-- Written by app/core/settingsdoc.py from the settings registry (app/core/personality.py, -->
<!-- read through app/core/settingrows.py). Regenerate with: -->
<!--     .venv/bin/python -m app.core.settingsdoc --write -->
<!-- tests/test_settingsdoc.py compares the committed file against what the registry renders, so -->
<!-- a registry change that skips this file is a failing test, not a stale reference. -->"""


def _fmt_value(value, unit: str | None) -> str:
    if value is None:
        return "— (the server decides)"
    return f"`{value}`" + (f" {unit}" if unit else "")


def _scope_words(scope: str) -> str:
    return _SCOPE_WORDS.get(scope, scope)


def _range_words(row: dict) -> str | None:
    """The numeric bounds, in words — only when they say more than the rule sentence does."""
    lo, hi = (row.get("range") or (None, None))[:2]
    if lo is None and hi is None:
        return None
    unit = f" {row['unit']}" if row.get("unit") else ""
    if lo is not None and hi is not None:
        return f"between `{lo}` and `{hi}`{unit}"
    if lo is not None:
        return f"`{lo}` or more{unit}"
    return f"up to `{hi}`{unit}"


def _squash(text: str) -> str:
    """Comparable form: letters and digits only, lower-cased.

    Used to stop the reference printing the rule sentence twice. The registry's `rule` is
    prose ("0 or more", "comma-separated of: none, draft-mtp, ..."); the structured fields say
    the same thing as data. Data is printed only where it adds something prose did not say.
    """
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# The registry's rule string for a switch is the word `bool`. Printed as-is, the reference
# would tell an operator that a switch accepts "bool" — which is a type, not a rule. The
# sentence below is about the measured behaviour of the preset layer, not an invention.
_BOOL_RULE = ("true or false — the setting is a switch, and false is the server's unset state, "
              "so writing false changes nothing (§6.12.26)")


def _accepted(row: dict) -> str:
    # `kind` is the row's human word ("switch"); `param_kind` is llama.cpp's (`bool`).
    rule_text = _BOOL_RULE if (row["param_kind"] == "bool" and _squash(row["rule"]) == "bool") \
        else row["rule"]
    parts = [rule_text]
    rule = _squash(row["rule"])
    if row.get("choices") and any(_squash(c) not in rule for c in row["choices"]):
        parts.append("values: " + ", ".join(f"`{c}`" for c in row["choices"]))
    if row.get("values") and any(_squash(v) not in rule for v in row["values"]):
        parts.append("aliases: " + ", ".join(f"`{v}`" for v in row["values"]))
    span = _range_words(row)
    if span and _squash(span) != rule:
        parts.append(f"range: {span}")
    if row.get("specials"):
        parts.append("specials: " + ", ".join(f"`{s}`" for s in row["specials"]))
    if row.get("accepted"):
        # A switch has no `choices`, so its words used to be invented at the control. They are the
        # runtime's now, and printing them is what makes "true or false" checkable.
        parts.append("spellings this portal accepts: " + ", ".join(f"`{a}`" for a in row["accepted"])
                     + " (a preset boolean is read by `is_truthy`; a word outside this list is false)")
    return " — ".join(p for p in parts if p)


def _glance_table(rows: list[dict]) -> list[str]:
    out = [
        "| key | scope | type | portal default | llama-server default |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        out.append(
            f"| `{r['key']}` | {_scope_words(r['scope'])} | {r['kind']} "
            f"({r['param_kind']}) | {_fmt_value(r['default'], r.get('unit'))} "
            f"| {_fmt_value(r.get('server_default'), r.get('unit'))} |"
        )
    return out


def _key_section(row: dict, resolved: dict[str, str] | None = None) -> list[str]:
    """One key's section. `resolved` fills the `{name}` placeholders a help line may quote.

    The row module refuses to expand a placeholder nothing answers, and the reference prints the
    same sentences the page does, so it takes the same refusal: `--write` failing on a help string
    that quotes a fact nothing resolves is better than a document that prints `{cache_ram_mib}`.
    """
    resolved = settingrows.substitutions() if resolved is None else resolved
    unit = row.get("unit")
    lines = [f"### {row['key']}", ""]
    facts = [
        f"- **scope**: {_scope_words(row['scope'])}",
        f"- **type**: {row['kind']} (llama.cpp parameter type `{row['param_kind']}`)",
        f"- **accepted**: {_accepted(row)}",
        f"- **portal default**: {_fmt_value(row['default'], unit)}",
        f"- **llama-server default**: {_fmt_value(row.get('server_default'), unit)}",
    ]
    if row.get("aliases"):
        facts.append("- **spellings on the command line**: "
                     + ", ".join(f"`{a}`" for a in row["aliases"]))
    if row.get("substitutions"):
        facts.append("- **written into `presets.ini` as**: "
                     + ", ".join(f"`{s}`" for s in row["substitutions"]))
    if row.get("omitted_when_equal_to") is not None:
        facts.append(
            f"- **the no-op line**: writing {_fmt_value(row['omitted_when_equal_to'], unit)} "
            "emits nothing — that is what the server already does, so the line would look like "
            "a change while changing nothing (§4.10.0)."
        )
    if row.get("preset_only"):
        facts.append("- **preset-only**: the server accepts this key through a preset file "
                     "(a section of `presets.ini`), not as a loose server argument.")
    elif row.get("preset_mapped"):
        facts.append("- **preset-mapped**: emitted under the name the preset layer expects.")
    if row.get("visible_when"):
        facts.append("- **offerable per model when** the model index reports "
                     + " + ".join(f"`{f}`" for f in row["visible_when"])
                     + " — on a model without it the row is noise, so the editor hides it.")
    if not row.get("writable"):
        facts.append("- **not writable here**: the router controls this key at launch, "
                     "so a setting for it would be overwritten.")
    lines.extend(facts)
    lines.append("")
    lines.append(settingrows.expand(row["help"], resolved).strip())
    if row.get("detail"):
        lines += ["", f"*Side effect.* {settingrows.expand(row['detail'], resolved).strip()}"]
    if row.get("cite"):
        lines += ["", f"*Evidence.* `{row['cite'].strip()}`"]
    lines.append("")
    return lines


def render(rows: list[dict] | None = None) -> str:
    """The whole document, exactly as it should be committed."""
    rows = settingrows.rows() if rows is None else rows
    from . import personality as P

    # Read from the code the document describes, so a change in behaviour is a regenerated
    # document rather than a sentence that used to be true.
    facts = settingrows.substitutions()
    form_refusal = P.check_setting("host", "127.0.0.1")
    out: list[str] = [_GENERATED_BANNER, "", "# Portal settings reference", ""]
    out += [
        "Every key the portal has a definition for — which is the same set of keys a personality",
        "can set, the settings editor can offer, and the generator validates against. The table",
        "is for scanning; each key's own section below carries the rule sentence, the side",
        "effect, the command-line spellings, the `presets.ini` name, and the measured finding",
        "that justifies the default.",
        "",
        "Written from the registry, so it cannot disagree with the form: the rows here are",
        "`app.core.settingrows.rows()`, the same rows the editor renders and the renderer",
        "validates (§6.14.1).",
        "",
        "## 1. How to read a key",
        "",
        "- **scope** — *global only* keys live in the router's own section of `presets.ini`;",
        "  *model only* keys are per-model sections; *global and per-model* keys can be set",
        "  either way, and the per-model value wins for that model.",
        "- **portal default** — what the portal writes when a personality does not override the",
        "  key. `—` means the portal writes nothing and lets the server decide.",
        "- **llama-server default** — what the binary on this box defaults to, read from its own",
        "  `--help` (see §6.12.26 for how that was measured). The two defaults are printed side",
        "  by side because the gap between them is the whole reason a portal writes a key at all.",
        "- **the no-op line** — a key whose value equals the server's default is *omitted* from",
        "  the generated file. `llama-server` treats an absent preset key and a false one the",
        "  same way, so writing the default is decoration (§4.10.0, measured against the build", "  in §6.12.26).",
        "- **offerable per model when** — the editor hides a row on a model whose index facts",
        "  say the setting does not apply, e.g. `sliding_window` for the checkpoint keys.",
        "- **not writable here** — a key the router owns (`host`, `port`, `api-key`, `alias`,",
        "  `model`, `model-draft`, `hf-repo`, `hf-file`) is set at launch and overwritten there,",
        "  so the editor never offers a control for one. These are **not registry keys**: nothing",
        "  in `SETTINGS` defines them, which is why §4 prints the refusals they do get and why",
        "  the form's answer is the short one.",
        "",
        "## 2. Keys at a glance",
        "",
    ]
    out += _glance_table(rows)
    out += ["", "## 3. Keys", ""]
    for row in rows:
        out += _key_section(row, facts)
    out += [
        "## 4. Keys the page will never offer",
        "",
        "The router passes these itself when it launches, so a personality that sets them is",
        "setting something that gets overwritten. Two boundaries refuse them, and they are not",
        "the same answer, so both are printed here rather than one of them:",
        "",
        "- **the settings form** refuses with the message a key nothing defines gets:",
        f"  `{form_refusal}`",
        "  They are absent from `SETTINGS`, so the form has no rule to check them against, and",
        "  the message is honest about not knowing — which is the difference between refusing and",
        "  accepting a value and then losing it.",
        "- **the generator** refuses the whole file. `personality.render()` raises before writing,",
        "  naming the keys and the reason (`the router overwrites them`), in the global section and",
        "  in a model section alike. That refusal is the one that protects `presets.ini`, and it",
        "  is guarded by tests, not by this document.",
        "",
        "`alias` is the one exception, and it is a narrow one: the portal writes it as a model",
        "config's *name*, never as a setting a person typed (§4.10.4).",
        "",
    ]
    out += [f"- `{k}`" for k in sorted(P.ROUTER_CONTROLLED_KEYS)]
    out += ["", "Derived by the router's own launch, not stored as a setting:", ""]
    out += [f"- `{k}`" for k in sorted(P.ROUTER_DERIVED_KEYS)]
    out += [""]
    return "\n".join(out).rstrip() + "\n"


def write(path: Path) -> Path:
    target = path if path.suffix == ".md" else path / DOC_NAME
    target.write_text(render(), encoding="utf-8")
    return target


def stale(path: Path) -> bool:
    """True when the committed document no longer matches the registry."""
    target = path if path.suffix == ".md" else path / DOC_NAME
    if not target.is_file():
        return True
    return target.read_text(encoding="utf-8") != render()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate docs/PORTAL_SETTINGS.md from the registry")
    ap.add_argument("--write", action="store_true", help="write the document")
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / DOC_NAME))
    args = ap.parse_args(argv)
    target = Path(args.out)
    if args.check:
        if stale(target):
            print(f"STALE: {target} does not match the registry — run --write")
            return 1
        print(f"current: {target}")
        return 0
    if args.write:
        path = write(target)
        print(f"wrote {path} ({len(render().splitlines())} lines, "
              f"{len(settingrows.rows())} keys)")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
