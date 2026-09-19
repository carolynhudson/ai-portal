"""Personalities (§4.7): a task-shaped allocation of the box, rendered as presets.ini.

A personality answers four questions the router cannot answer for itself: which models
may be loaded at all, how many at once, what each is configured with, and what the worst
case costs. `presets.ini` is one *output* of that decision, never the decision itself.

This module is deliberately read-only. It generates text and a diff; it does not write
`presets.ini`. N4 is the first step in this project that changes anything the router
reads, so writing is a separate, explicit call that goes through the gated restart job
and records into `presets_history` first.

Why the budget is the portal's job and nobody else's:
  * the router evicts under its own queue pressure and otherwise keeps a loaded model
    forever;
  * `fit` is blind to what is already resident;
  * loaded weights are anonymous and unreclaimable;
  * a model's real footprint runs well above its file size.
So "the three biggest files fit" is not a budget. The budget here is the *heaviest
models_max combination in the set*, computed from modelindex's peak figure, and it is
reported with its inputs rather than as a bare yes/no.

Two personalities always exist and are not editable in place (see SAFE / DEFAULT below).
"""
from __future__ import annotations

import configparser
import difflib
import time
from dataclasses import dataclass, field
from typing import Any

from app.core import modelindex, paths

# The router's own preset-exclusive options, from llama.cpp tools/server/README.md.
# These are NOT command-line arguments and only exist inside a preset file, so they are
# the levers the portal has that the router CLI does not.
ROUTER_ONLY_KEYS = ("load-on-startup", "stop-timeout", "dedup-cache-models")

# Keys the router strips or overwrites when it launches an instance. Putting one here
# would silently do nothing, so the generator refuses to emit it.
ROUTER_CONTROLLED_KEYS = frozenset({
    "host", "port", "api-key", "api-key-file", "alias", "model", "model-draft",
    "hf-repo", "hf-file",
})

#: Global section. Values here are what every model inherits unless a personality
#: overrides it per model; the router's precedence is CLI > model section > [*].
GLOBAL_DEFAULTS: dict[str, Any] = {
    "threads": 16,
    "mmap": 1,
    "fit": "on",
    "fit-target": 8192,
    "stop-timeout": 15,
}

#: `safe` — the thing to fall back to. Minimal on purpose: one small model, nothing
#: else resident. If this one cannot start, the box is broken and the portal says so.
SAFE = {
    "name": "safe",
    "models_max": 1,
    "note": "fallback state; smallest known-good model only",
    "models": ["Qwen3.5-0.8B.Q4_K_M"],
}

#: `default` — the whole library, one resident at a time. Behaviour comes from the
#: global section only, which is why it carries no per-model overrides: an unlisted edit
#: to presets.ini is how the next person debugging an OOM loses their afternoon.
DEFAULT = {
    "name": "default",
    "models_max": 1,
    "note": "every llama model in the library, one resident at a time",
    "models": None,  # None = the whole library, resolved from the index at render time
}


# Tri-state result for "does the worst case fit". None would work but reads as an
# accidental gap; named constants make the third state deliberate at every branch.
FITS_YES = True
FITS_NO = False
FITS_UNKNOWN = None


class PersonalityError(Exception):
    """A personality that cannot be honoured: unknown model, unpriceable model, or a
    worst case that does not fit. Raised rather than clamped, because a silently reduced
    personality is a personality nobody asked for."""


def _models_max(personality: dict) -> int:
    """models_max as an int, or a PersonalityError naming the bad value.

    int("3") is fine and int(3.7) is fine, but int("three") raises ValueError with a
    message about literals, which is not the error an operator needs. Every caller here
    sits on a request path, so a hostile or hand-edited personality dict must surface as
    our own error rather than a 500.
    """
    raw = personality.get("models_max")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PersonalityError(
            f"personality {personality.get('name')!r}: models_max must be a whole "
            f"number, got {raw!r}"
        ) from None
    if value < 1:
        raise PersonalityError(
            f"personality {personality.get('name')!r}: models_max must be >= 1, got {value}"
        )
    return value


def _gib(value: int | None) -> str:
    """GiB for a message, with None rendering as a claim of ignorance.

    worst_case_bytes and budget_bytes are both Optional on purpose: an unpriceable model
    or an unreadable aperture means there is no number. Formatting None would raise
    inside the function whose whole job is to explain why there is no number.
    """
    if value is None:
        return "unknown"
    return f"{value / 2 ** 30:.1f} GiB"


@dataclass
class Budget:
    """The worst-case cost of a personality, with the inputs that produced it.

    `worst_case_bytes` is not the sum of the three biggest files: it is the heaviest
    `models_max`-sized combination the router could end up holding. That distinction is
    the whole point — with models_max=3 and one 87 GiB model in the set, the budget is
    that model plus the two next heaviest, and if it does not fit, the personality is
    wrong, not the math.
    """

    models_max: int
    priced: list[dict] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    worst_case_bytes: int | None = None
    heaviest: list[str] = field(default_factory=list)
    budget_bytes: int | None = None
    headroom_bytes: int | None = None
    fits: bool | None = FITS_UNKNOWN

    def as_dict(self) -> dict:
        return {
            "models_max": self.models_max,
            "priced": len(self.priced),
            "unpriced": self.unpriced,
            "worst_case_bytes": self.worst_case_bytes,
            "heaviest": self.heaviest,
            "budget_bytes": self.budget_bytes,
            "headroom_bytes": self.headroom_bytes,
            "fits": self.fits,
        }


def _rows() -> list[dict]:
    """Library rows from the index. Never rescan here: personality generation happens
    on a request path, and a scan of the model tree does not belong on one."""
    return modelindex.rows()


def resolve_models(personality: dict) -> list[str]:
    """Expand a personality's `models` (None = whole library) to real directory names.

    Unknown names raise. A personality that quietly drops a model the operator named is
    worse than one that refuses: the operator would see a working-looking box missing the
    model they were about to rely on.
    """
    known = {r["id"] for r in _rows()}
    named = personality.get("models")
    if named is None:
        return sorted(known)
    unknown = [m for m in named if m not in known]
    if unknown:
        raise PersonalityError(
            f"personality {personality.get('name')!r} names models with no library "
            f"entry: {', '.join(unknown)}"
        )
    return list(named)


def budget_for(personality: dict, budget_bytes: int | None = None) -> Budget:
    """Compute the worst-case resident cost of a personality.

    The heaviest `models_max` combination is just the top `models_max` peak figures: any
    other subset is lighter, so the maximum is achieved by taking the largest ones. What
    is NOT done here is substituting file size for peak cost — a model's footprint runs
    materially above its file size, and modelindex's peak figure already carries weights
    + KV at the context it will actually get + runtime headroom.
    """
    models_max = _models_max(personality)

    pool = budget_bytes if budget_bytes is not None else modelindex._pool_bytes()
    # modelindex._pool_bytes() falls back to 0, not None. 0 is "could not measure",
    # which must not become "this personality does not fit": the same reason verdict()
    # returns 'unknown' rather than 'never' when the pool is unreadable.
    if not pool:
        pool = None
    rows = {r["id"]: r for r in _rows()}
    out = Budget(models_max=models_max, budget_bytes=pool)

    peaks: list[tuple[int, str]] = []
    for mid in resolve_models(personality):
        row = rows.get(mid)
        if row is None:
            out.unpriced.append(mid)
            continue
        peak = modelindex.peak_bytes(row)
        if peak is None:
            # Unpriceable is not "free": it means we cannot prove this fits.
            out.unpriced.append(mid)
            continue
        peaks.append((peak, mid))
        out.priced.append({"id": mid, "peak_bytes": peak,
                           "est_ctx": row.get("est_ctx")})

    peaks.sort(reverse=True)
    take = peaks[:models_max]
    out.heaviest = [mid for _, mid in take]

    if out.unpriced:
        # Cannot bound the worst case, so cannot claim it fits. `fits=None`, not True.
        out.fits = None
        return out
    if not take:
        out.fits = None
        return out

    out.worst_case_bytes = sum(p for p, _ in take)
    if pool is None:
        out.fits = None
        return out
    out.headroom_bytes = pool - out.worst_case_bytes
    out.fits = out.worst_case_bytes <= pool
    return out


def _ini_text(sections: dict[str, dict[str, Any]]) -> str:
    """Render an ini body. Comments carry the evidence, because a bare number in a file
    the router reads is exactly the thing a future debugger cannot question.

    Ordering is explicit and stable: [*] first (the router applies it as the base), then
    model sections sorted by name, so a diff between two generations is a diff of intent
    and not of dict ordering.
    """
    lines = [
        "# Generated by ai-portal app/core/personality.py",
        "# Section names MUST match directory names under ~/ai/models/llama.",
        "# Keys are llama-server args (no leading dash); see llama.cpp tools/server/README.md.",
        "# Router-only keys understood here: " + ", ".join(ROUTER_ONLY_KEYS),
        "",
    ]
    for section in sorted(sections, key=lambda s: (s != "*", s)):
        body = sections[section]
        lines.append(f"[{section}]")
        for key in sorted(body):
            lines.append(f"{key} = {body[key]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(personality: dict, budget_bytes: int | None = None) -> dict:
    """Generate presets.ini text for a personality, plus the budget it implies.

    Read-only. Returns the text, the diff against what is on disk, and the budget with
    its inputs. Writing is `apply()`'s job and does not exist yet.
    """
    name = personality.get("name")
    if not name:
        raise PersonalityError("personality needs a name")

    models_max = _models_max(personality)
    models = resolve_models(personality)
    overrides = personality.get("overrides") or {}

    sections: dict[str, dict[str, Any]] = {"*": dict(GLOBAL_DEFAULTS)}
    for mid in models:
        per = dict(overrides.get(mid) or {})
        hostile = sorted(k for k in per if k.lower() in ROUTER_CONTROLLED_KEYS)
        if hostile:
            # The router removes or overwrites these on load; emitting them would look
            # like a setting and do nothing.
            raise PersonalityError(
                f"personality {name!r} sets router-controlled key(s) {hostile} for "
                f"{mid}; the router overwrites them"
            )
        if per:
            sections[mid] = per

    text = _ini_text(sections)
    current = _current_text()
    return {
        "name": name,
        "models": models,
        "models_max": models_max,
        "text": text,
        "current": current,
        "changed": text != current,
        "diff": list(difflib.unified_diff(
            (current or "").splitlines(), text.splitlines(),
            fromfile="presets.ini (on disk)", tofile=f"presets.ini ({name})",
            lineterm="")),
        "budget": budget_for(personality, budget_bytes).as_dict(),
    }


def _current_text() -> str:
    try:
        return paths.PRESETS_INI.read_text()
    except OSError:
        return ""


def parse_current() -> dict[str, dict[str, str]]:
    """Read what the router is using right now. Returns {} when the file is missing or
    unparseable rather than raising, so a broken presets.ini degrades the personality
    page instead of taking the portal down with it."""
    cp = configparser.ConfigParser(allow_no_value=True, strict=True,
                                   comment_prefixes=("#", ";"))
    try:
        cp.read_string(_current_text())
    except Exception:
        return {}
    return {s: dict(cp[s]) for s in cp.sections()}


def builtins() -> list[dict]:
    """The two personalities that always exist and are never edited in place."""
    return [dict(SAFE), dict(DEFAULT)]


def validate(personality: dict, budget_bytes: int | None = None) -> list[dict]:
    """Findings about a personality, severity-first. A personality that does not fit its
    budget is a `bad` finding with the numbers attached, not a refusal to render.
    """
    findings: list[dict] = []
    try:
        b = budget_for(personality, budget_bytes)
    except PersonalityError as exc:
        return [{"severity": "bad", "kind": "personality", "message": str(exc)}]

    if b.unpriced:
        findings.append({
            "severity": "warn", "kind": "personality",
            "message": f"{len(b.unpriced)} model(s) cannot be priced, so the worst case "
                       f"is unbounded: {', '.join(b.unpriced[:4])}",
        })
    # fits is tri-state, so branch on the value rather than on truthiness:
    # FITS_NO = measured and over budget, FITS_YES = measured and inside it,
    # FITS_UNKNOWN = we could not measure. `if not b.fits` would report the unmeasured
    # case as a failure, which is the exact collapse verdict() warns about.
    if b.fits is FITS_NO:
        findings.append({
            "severity": "bad", "kind": "personality",
            "message": (
                f"worst case {_gib(b.worst_case_bytes)} exceeds the "
                f"{_gib(b.budget_bytes)} aperture: with models_max="
                f"{b.models_max} the router could hold {', '.join(b.heaviest)} at once"
            ),
        })
    if b.fits is FITS_YES:
        findings.append({
            "severity": "ok", "kind": "personality",
            "message": (
                f"worst case {_gib(b.worst_case_bytes)} fits with "
                f"{_gib(b.headroom_bytes)} spare ({', '.join(b.heaviest)})"
            ),
        })
    # Not gated on `not b.unpriced`. An unpriceable model and an unreadable aperture are
    # two independent facts, and the first one masks the second if the condition is
    # shared: with both true, a reader was told only about the model and never learned the
    # box itself could not be measured. Each finding stands on its own condition.
    if b.budget_bytes is None:
        findings.append({
            "severity": "warn", "kind": "personality",
            "message": "no readable memory budget, so the worst case cannot be judged",
        })
    return findings


def describe(personality: dict, budget_bytes: int | None = None) -> dict:
    """A personality as the page shows it: identity, models, budget, findings."""
    name = personality.get("name", "?")
    out = {
        "name": name,
        "note": personality.get("note") or "",
        "models_max": 1,
        "editable": name not in ("safe", "default"),
        "findings": [],
        "generated_at": time.time(),
    }
    # One try for the whole evaluation: models_max parsing, model resolution and budget
    # math can all raise, and describe() renders a page. A half-built dict with a valid
    # models_max and no findings would read as "this personality is fine".
    try:
        out["models_max"] = _models_max(personality)
        out["models"] = resolve_models(personality)
        out["findings"] = validate(personality, budget_bytes)
    except PersonalityError as exc:
        out["models"] = []
        out["error"] = str(exc)
        out["findings"] = [{"severity": "bad", "kind": "personality", "message": str(exc)}]
    return out
