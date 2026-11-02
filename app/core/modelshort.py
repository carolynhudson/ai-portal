"""Shorten model names for narrow screens, with collision refusal. (Owner, 2026-11-02.)

The status hero shows the router's loaded model, and
`ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S` does not fit a phone -
it wraps into an unreadable three-line monster (the viewport probe photographed
it). The owner's reduction of that exact name is the spec for this module:

    ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S
    -> Swift-Qwen3.8-FN:IQ3_S

Why each piece goes: the hub org prefix (`ukisai/`) is not said aloud; the
family lead (`Swift`) stays because it is the name's identity; `1.5` goes
because every model in that family group carries it; `GSQ`, `RCO` go for the
same shared-within-group reason; `GGUF` goes because every model llama.cpp
loads is GGUF; `Flash-Next` becomes `FN` through an explicit phrase map; and
the quant after the colon is **always** kept, because that is the part that
distinguishes two files of the same model. The owner's phrase: redundancy is
judged *inside the group of models sharing a family lead*, not across the
whole library - `Qwen3.8` is shared by the Swift models and stays, because it
identifies the base architecture.

**This module is measurement, not deployment.** Nothing in the UI imports it
yet; the owner's rule is that no display change ships until the layout
measures have been read and the need is understood. What ships here is the
capability plus its tests, and the collision law: `reduce_names` shortens a
whole library together, and any two models that would collapse to the same
short name re-admit their distinguishing tokens until they do not - a
reducer that makes two models read the same is worse than a long name.
"""
from __future__ import annotations

import re

# Container formats llama.cpp always implies: never said aloud.
# Quant METHODS (AWQ, GPTQ, ...) are NOT here: they distinguish quant
# families, so they are never redundant, in any group.
FORMAT_TAGS = {"GGUF", "GGML", "SAFETENSORS"}
QUANT_METHODS = {"AWQ", "GPTQ", "EXL2", "EXL3", "BNB", "NF4", "FP8", "FP16", "BF16"}

# Explicit phrase abbreviations, in order, longest-first at match time.
# The owner coined FN for Flash-Next; new phrases are added only with a test.
ABBREV: dict[tuple[str, ...], str] = {
    ("Flash", "Next"): "FN",
    ("Flash", "Lite"): "FL",
    ("Vision", "Language"): "VLM",
}

# A token that identifies the base architecture: a capitalized name carrying a
# version digit - Qwen3.8, Nova2, Phi4, Llama3.2. Architecture is meaning:
# shared-within-group redundancy never eats it.
ARCH_RX = re.compile(r"^[A-Z][A-Za-z]*[0-9][0-9.]*$")

# A tuning/structure tag: all caps, 2-5 letters (GSQ, RCO, DPO, LoRA...).
TAG_RX = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")


def _split(name: str) -> tuple[str, str, str]:
    """(org, stem, quant-suffix). The suffix keeps its colon; the org keeps
    its slash; both may be empty. The hub `repo:TAG` shape (mesh names) takes
    the tag as the quant, which is exactly right: it is the distinguisher."""
    head = name
    quant = ""
    if ":" in head:
        head, q = head.rsplit(":", 1)
        quant = ":" + q
    org = ""
    if "/" in head:
        org, _ = head.split("/", 1)
        org += "/"
    return org, head[len(org):] if org else head, quant


def _group_key(stem: str) -> str:
    return stem.split("-")[0]


def _reduce_stem(stem: str, group_tokens: set[str]) -> str:
    """One name inside its family group. `group_tokens` are the tokens every
    member of the group carries; shared-within-group tokens are redundant
    except the lead, architecture, and the quant (handled by the caller)."""
    toks = stem.split("-")
    lead, rest = toks[0], toks[1:]
    out: list[str] = [lead]
    i = 0
    while i < len(rest):
        t = rest[i]
        # phrase abbreviations first: they consume tokens in order
        hit = None
        for phrase in sorted(ABBREV, key=len, reverse=True):
            if tuple(rest[i:i + len(phrase)]) == phrase:
                hit = (phrase, ABBREV[phrase])
                break
        if hit:
            out.append(hit[1])
            i += len(hit[0])
            continue
        if t in FORMAT_TAGS:
            i += 1
            continue
        if t in QUANT_METHODS:                # quant families: never redundant
            out.append(t)
            i += 1
            continue
        if ARCH_RX.match(t):
            out.append(t)
            i += 1
            continue
        if t in group_tokens:                 # shared within the family
            i += 1
            continue
        if TAG_RX.match(t) and t in group_tokens:
            i += 1
            continue
        out.append(t)
        i += 1
    return "-".join(out)


def reduce_names(names: list[str]) -> dict[str, str]:
    """Shorten every name in a library together. Returns {full: short};
    no two fulls share a short (collision law)."""
    stems = {n: _split(n)[1] for n in names}
    groups: dict[str, list[str]] = {}
    for n, stem in stems.items():
        groups.setdefault(_group_key(stem), []).append(n)

    short: dict[str, str] = {}
    for members in groups.values():
        if len(members) == 1:
            # Redundancy is sharedness, and a group of one shares with nobody:
            # an only member loses nothing but formats and abbreviations.
            # (The first live-library run proved the trap: "L3-8B-Stheno-v3.2"
            # reducing to "L3" is compression that destroys the name.)
            shared: set[str] = set()
        else:
            shared = set(stems[members[0]].split("-"))
            for m in members[1:]:
                shared &= set(stems[m].split("-"))
        for m in members:
            org, stem, quant = _split(m)
            s = _reduce_stem(stem, shared) + quant
            short[m] = s

    # collision law: a shared short re-admits distinguishing tokens, in the
    # original token order, for the members that have them, until unique.
    by_short: dict[str, list[str]] = {}
    for full, s in short.items():
        by_short.setdefault(s, []).append(full)
    for s, members in by_short.items():
        if len(members) < 2:
            continue
        for m in members:
            stem = stems[m]
            quant = _split(m)[2]
            toks = stem.split("-")
            rebuilt = None
            for k in range(1, len(toks)):
                cand = "-".join(toks[:k + 1]) + quant
                others = {v for kk, v in short.items() if kk != m}
                if cand not in others and cand not in by_short:
                    rebuilt = cand
                    break
            short[m] = rebuilt or stem + quant   # last resort: full stem
    return short


def reduce_one(name: str) -> str:
    """A single name, reduced in isolation (its own group of one: nothing is
    shared-within-group, so only org/format/abbrev rules apply). The library
    view is `reduce_names`; this is the convenience for one-off display."""
    if not isinstance(name, str) or not name.strip():
        return name if isinstance(name, str) else ""
    return reduce_names([name])[name]
