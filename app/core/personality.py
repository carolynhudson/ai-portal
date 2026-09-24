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

from app.core import db, modelindex, modelmeta, paths

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

#: What llama-server itself defaults to, so "this setting is a choice" is decidable rather
#: than a matter of taste. Read from the binary this box actually runs — `llama-server --help`,
#: build b10964 — with the two router-only keys from source: `DEFAULT_STOP_TIMEOUT 10`
#: (tools/server/server-models.cpp:43) and `models-max` 4. `parallel -1` is the registered
#: default even though auto resolves to 4 slots over a unified KV pool, because writing 4 pins
#: a number a future release may change while -1 lets the server keep deciding.
#:
#: A value that equals the server's own default is not documentation, it is a fork of that
#: release's number: the day llama.cpp changes a default, a pinned copy silently becomes the
#: setting. So the generator omits them, and `render()` reports what it omitted rather than
#: dropping it silently.
SERVER_DEFAULTS: dict[str, Any] = {
    "threads": -1,
    "ctx-size": 0,             # 0 = loaded from the model
    "flash-attn": "auto",
    "parallel": -1,
    "ctx-checkpoints": 32,
    "cache-prompt": True,
    "reasoning-budget": -1,
    "reasoning-effort": "default",
    "n-gpu-layers": "auto",
    "cache-reuse": 0,
    "jinja": True,
    "lazy-mode": "auto",
    "stop-timeout": 10,
    "models-max": 4,
}


#: Global section. Values here are what every model inherits unless a personality
#: overrides it per model; the router's precedence is CLI > model section > [*].
# `fit` and `fit-target` were here and are gone, on measurement rather than preference.
# `fit_probe.py` (§6.12.2) showed `fit` is blind to memory other processes already hold —
# 18 GB held elsewhere changed the context it chose by nothing at all — and that it does
# not cap the model's own cost either: an 87.2 GB model occupied ~98 GB under
# `--fit-target 8192`. A guard that neither refuses an over-committed set nor bounds the
# model it is applied to is not a safety net, and the owner had already deleted both keys
# from the live file by hand. Re-adding them here would have been the portal silently
# undoing a decision made with better evidence than the registry had.
GLOBAL_DEFAULTS: dict[str, Any] = {
    "threads": 16,
    "lazy-mode": "on",
    "stop-timeout": 15,
}

# --- the settings registry -----------------------------------------------------
#
# Every key the portal is willing to name, with what it means and what a legal value
# looks like. Two separate things live here and both are load-bearing:
#
#   * `help` and `kind` are what a settings editor renders — a control matched to the
#     value type, not a free-text box. Building the registry first means the editor is a
#     rendering pass over data instead of a second, weaker statement about what each key
#     does.
#   * `accepts()` is what validation uses today, so a generated presets.ini is checked
#     against the same definition the UI will show rather than against a regex written
#     later for the form.
#
# Definitions are quoted from llama.cpp's own tools/server/README.md and cross-checked
# against what the live router resolves (GET /models -> status.preset, 17/17 models), not
# guessed from habit. `mmap` and `mlock` are deliberately absent: they appear in no preset
# the router reports and in no server argument list, so naming them here would invent a
# setting.
#
# `writable` marks a key the portal may emit. `metrics`, `model` and `mmproj` are present
# in every preset the router reports but the router derives them itself — it writes the
# section header, the model path and the projector path — so emitting them is the portal
# asserting something the router has already decided.
SETTINGS: dict[str, dict[str, Any]] = {
    "threads": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "CPU threads used during generation. -1 lets llama.cpp pick. The global "
                "section on this box sets 16.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "fit": {
        "kind": "choice",
        "section": "both",
        "default": "on",
        "choices": ("on", "off"),
        "help": "Adjust arguments that were not set so the model fits in device memory. "
                "Blind to what is already resident: it fits against free memory at load "
                "time, not against a plan.",
        "accepts": lambda v: v in ("on", "off"),
    },
    "fit-target": {
        "kind": "int",
        "section": "both",
        "default": 1024,
        "help": "Margin in MiB the fit adjustment leaves per device. Comma-separated for "
                "a per-device list; one value applies to every device.",
        "accepts": lambda v: _positive_mib_list(v),
    },
    "stop-timeout": {
        "kind": "int",
        "section": "both",
        "default": 10,
        "preset_only": True,
        "help": "Seconds to wait after a requested unload before forcing termination. "
                "Preset-exclusive: it is not a command-line argument at all.",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "load-on-startup": {
        "kind": "bool",
        "section": "model",
        "default": False,
        "preset_only": True,
        "help": "Load this model when the server starts rather than on first request. "
                "Only read at startup: after a preset reload a newly added model is "
                "listed but not loaded.",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "dedup-cache-models": {
        "kind": "bool",
        "section": "both",
        "default": False,
        "preset_only": True,
        "help": "When a preset's hf-repo points at a model already downloaded, hide the "
                "cached entry from GET /models. Preset-exclusive.",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "ctx-size": {
        "kind": "int",
        "section": "model",
        "default": 0,
        "help": "Prompt context. 0 means take it from the model. Not emitted by default: "
                "a hard-coded ctx cap below a model's trained context silently removes "
                "capability, which is what the old per-model sections on this box did.",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "n-gpu-layers": {
        "kind": "choice-or-int",
        "section": "both",
        "default": "auto",
        "help": "How many layers live in VRAM: a number, 'auto', or 'all'. The lever that "
                "trades GPU for system RAM on an oversized model.",
        "accepts": lambda v: v in ("auto", "all") or (isinstance(v, int) and v >= 0),
    },
    "flash-attn": {
        "kind": "choice",
        "section": "both",
        "default": "auto",
        "choices": ("on", "off", "auto"),
        "help": "Flash Attention. On this box it is the setting most likely to matter for "
                "large-context models on ROCm.",
        "accepts": lambda v: v in ("on", "off", "auto"),
    },
    "parallel": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "Number of request slots. -1 = auto, and auto resolves to 4 slots with a "
                "unified KV pool (server.cpp), so the pool is shared rather than copied per "
                "slot. Set it explicitly, or price KV per slot only when KV is not unified.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "ctx-checkpoints": {
        "kind": "int",
        "section": "both",
        "default": 32,
        "help": "Context checkpoints kept per slot (server default 32). Each one holds a "
                "saved slice of the cache, so this is memory that shows up *after* a request "
                "finishes, not at load time — which is why RSS climbs when a model looks idle.",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "cache-prompt": {
        "kind": "bool",
        "section": "both",
        "default": True,
        "help": "Keep finished slot states in the prompt cache so a similar next request is "
                "faster. This is where the OOM usually lands on this box: upstream reported an "
                "idle slot saving a 2.6 GiB state and the kernel killing the worker five "
                "seconds later (llama.cpp discussion #22372), on the same machine class.",
        "accepts": lambda v: isinstance(v, bool),
    },
    "reasoning-budget": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "Token budget for thinking: -1 unrestricted (the default), 0 stops thinking "
                "immediately. Reasoning tokens are the largest source of prompt growth in a "
                "long session, and prompt growth is what makes checkpoints and prompt-cache "
                "saves expensive. It changes what a session costs, not what a model costs to "
                "load.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "reasoning-effort": {
        "kind": "choice",
        "section": "both",
        "default": "default",
        "choices": ("default", "minimal", "low", "medium", "high", "xhigh", "max"),
        "help": "Reasoning level handed to the chat template. This is the knob Qwen3.8-Flash-"
                "Next actually obeys: its template reads `reasoning_effort` and branches on "
                "'low' / 'medium' / 'high' / 'xhigh'.",
        "accepts": lambda v: v in ("default", "minimal", "low", "medium", "high", "xhigh", "max"),
    },
    "jinja": {
        "kind": "bool",
        "section": "both",
        "default": True,
        "help": "Use the model's embedded chat template. Tool calling needs it: a model "
                "whose template declares tools is unusable with jinja off.",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "ctx-checkpoints": {
        "kind": "int",
        "section": "both",
        "default": 32,
        "help": "Context checkpoints kept per slot. Costs memory per slot.",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "keep": {
        "kind": "int",
        "section": "both",
        "default": 0,
        "help": "Tokens kept from the initial prompt. -1 keeps all.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "lazy-mode": {
        "kind": "choice",
        "section": "both",
        "default": "auto",
        "choices": ("on", "off", "auto"),
        "help": "Read large tensors from disk on demand instead of keeping them resident. "
                "`auto` (the server default) applies it only above 4 GiB; `on` applies it to "
                "every such tensor and requires mmap. On this box it is the key most likely to "
                "matter for the multi-hundred-GiB models: keeping weights resident is what "
                "competes with the GTT aperture. Not emitted by default, because a router started "
                "by hand can already set it on the command line, and CLI beats presets.ini.",
        "accepts": lambda v: v in ("on", "off", "auto"),
    },
}

# Present in every preset the router reports, but written by the router, not by us.
ROUTER_DERIVED_KEYS = frozenset({"metrics", "model", "mmproj"})


# `lazy-mode` was registered but withheld from the defaults, on the reasoning that a
# hand-started router may already pass `--lazy-mode` on the CLI — where it outranks
# presets.ini anyway — so emitting it would look like a change while being overridden.
# That reasoning was sound about precedence and wrong about the managed router, which is
# the only router the portal controls and which passes no `--lazy-mode` at all
# (LLAMA_ARGS is empty in llamacpp.env). Measured on the same model and build (b10964):
# aperture used 80.5 GiB with lazy-mode unset against 54.5 GiB with it on, because `on`
# reads tensor rows from disk on demand instead of keeping them resident, which keeps the
# weights in reclaimable page cache instead of pinned anonymous buffers. The OOM kills in
# the journal all carry `file-rss: 8 kB` — never page-cache backed. Emitting it is the
# point; the CLI-overrides-preset fact is why the preview diff must be read as a plan for
# the managed router, not as a claim about whatever is running right now.


def _positive_mib_list(value: Any) -> bool:
    """fit-target takes one MiB value or a comma-separated list of them.

    Parsed rather than pattern-matched because the documented shape is a list and a
    hand-written regex tends to accept `1024,` or `1024,,2048` and then pass the string to
    a parser that rejects it at launch, which is the failure mode this validation exists
    to prevent. Non-integer entries are refused here, not by llama.cpp.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if not isinstance(value, str):
        return False
    parts = [p.strip() for p in value.split(",")]
    if not parts or any(p == "" for p in parts):
        return False
    try:
        return all(int(p) > 0 for p in parts)
    except ValueError:
        return False


def setting_names() -> list[str]:
    """Every key the portal has a definition for, in declaration order."""
    return list(SETTINGS)


def setting_is_writable(key: str) -> bool:
    """Can the portal emit this key at all?

    Three refusals, in order of how likely they are to be hit: a key with no definition
    (nobody has said what it means); a key the router strips or overwrites when it
    launches (emitting it would look like a setting and do nothing); a key the router
    derives itself (emitting it would restate a decision the router already made).
    """
    if key not in SETTINGS:
        return False
    return key not in ROUTER_CONTROLLED_KEYS and key not in ROUTER_DERIVED_KEYS


def coerce_setting(key: str, value: Any) -> Any:
    """Turn an ini/form value into the type this key's definition expects.

    `presets.ini` has no types: everything arrives as a string, so `threads = 16` reaches
    a validator as '16'. Rejecting that — which the first version of this function did,
    failing 34 out of 34 values the live router itself reports — is not strictness, it is
    the validator disagreeing with the file format it validates. The coercion is where the
    string becomes an int, and it is deliberately not permissive about the rest:
    '16.5' and 'sixteen' both stay strings and are then rejected.

    Booleans are coerced from 1/0 because that is what the ini holds. They are NOT coerced
    from 'yes'/'no'/'true': llama.cpp's own spellings differ per option (`fit = on|off`,
    flags written as 1), and guessing which word a given option wants is how a generated
    file ends up setting something other than what was intended.
    """
    spec = SETTINGS.get(key)
    if spec is None:
        return value
    kind = spec["kind"]
    if not isinstance(value, str):
        return value
    text = value.strip()
    if kind in ("int", "choice-or-int"):
        try:
            return int(text)
        except ValueError:
            return value  # left as a string so 'auto'/'all' still pass, and junk fails loudly
    # `bool` is not coerced at all. presets.ini spells these as 1/0 and the accepters
    # already take 1/0, so there is nothing to convert — and converting here would mean
    # deciding which spelling a given llama.cpp option wants. `fit` takes on/off, preset
    # flags take 1; guessing wrong sets something other than what was intended.
    return text


def check_setting(key: str, value: Any) -> str | None:
    """None if the value is legal for this key, else a message naming the key and value.

    Returns a message instead of raising because a settings form wants to show every
    problem at once, not the first one. The generated-file path turns these into
    PersonalityError and refuses to emit at all.

    A key the router owns is refused even when its value would be perfectly valid, so the
    check is done before the value is looked at: `alias = 3` should be reported as "the
    router sets this", not as "3 is not a valid alias".
    """
    spec = SETTINGS.get(key)
    if spec is None:
        return f"{key}: no definition for this setting"
    if not setting_is_writable(key):
        return f"{key}: controlled by the router, not editable here"
    value = coerce_setting(key, value)
    try:
        ok = bool(spec["accepts"](value))
    except Exception:  # a broken accepts() is a bug in this file, not bad user input
        return f"{key}: validator failed on {value!r}"
    if not ok:
        hint = spec.get("choices")
        allowed = f" one of {', '.join(map(str, hint))}" if hint else ""
        return f"{key}: {value!r} is not a valid value (expects {spec['kind']}{allowed})"
    return None

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


def _ini_value(value: Any) -> str:
    """Render one preset value the way llama.cpp reads it.

    A Python bool would otherwise land as `True` / `False`. llama.cpp's preset parser takes its
    own spellings (`on` / `off`), not Python's capitalisation, so a boolean offered by the editor
    would produce a file the router might reject at load time — which is the failure shape the
    whole presets pipeline exists to prevent.
    """
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def _conservative_peak(row: dict, ctx: int) -> int | None:
    """What this model would cost at `ctx` when the index cannot reprice it exactly.

    `peak_bytes()` can only price a context it has geometry for, and `model_index` does not keep
    all of it: the table stores `layers`, `kv_heads` and `attention_heads`, while the estimator
    works from sniffed GGUF metadata (`block_count`, `attention_kv_count`, `attention_count`) and
    a **head width** the table never stores. So `kv_bytes_at(row, other_ctx)` returns None, and
    `peak_bytes` has only ever worked when the requested context equals `est_ctx` and the stored
    `est_kv` can be reused. Repricing at a different context is precisely what an explicit
    `ctx-size` decision needs.

    The direction of the error is the entire question, and it is not the intuitive one. Scaling the
    stored cache linearly **understates** a sliding-window model whenever the requested context is
    smaller than the estimate's. Measured on this box, not theorised: `gemma-4-31B-it-QAT-Q4_0` is
    priced at 7.075 GiB of cache at 262144, and at 8192 it really costs 0.718 GiB while a linear
    scale reports 0.221 GiB — 3.2x too small, in the one direction a capacity check must never be
    wrong. Cache size is concave in ctx (window-capped layers hold `min(ctx, window)`), so it lies
    *above* the line from the origin to the stored point, and that line is below the truth
    everywhere beneath it.

    So the fallback never goes below the stored estimate: `max(stored, scaled)`. Above the estimate
    the linear scale overstates, which is safe; below it the stored value is kept, which is also
    safe. Both directions err toward refusing a context that would have fitted -- the one error a
    loader cannot survive is the one that says a model costs less than it does.
    """
    weights = modelmeta._as_int(row.get("est_weights"))
    kv = modelmeta._as_int(row.get("est_kv"))
    at = modelmeta._as_int(row.get("est_ctx"))
    # `kv is None` and `kv == 0` are different facts. Zero is a priced answer; None is the absence
    # of one, and treating them alike turns "we could not measure" into "costs nothing".
    if not weights or kv is None or not at or ctx < 1:
        return None
    return weights + max(kv, kv * ctx // at) + modelindex.HEADROOM_BYTES


def _no_op_reason(key: str, value: Any, row: dict | None) -> str | None:
    """Say why writing this key with this value is indistinguishable from not writing it.

    Two shapes. The obvious one: the value is llama-server's own default, so the line is a copy
    of this release's number rather than a decision — and the day the release changes it, the
    pinned copy becomes the setting. The subtler one is `ctx-size`, where both `0` and the
    model's trained maximum ask for exactly what the server would have chosen anyway; the second
    is what an editor reaches for by reflex when it wants to be explicit about a model's limit.
    """
    if key == "ctx-size":
        ctx = modelmeta._as_int(value)
        trained = modelmeta._as_int((row or {}).get("ctx_trained"))
        if ctx == 0:
            return "ctx-size = 0 already means 'loaded from the model'"
        if trained and ctx == trained:
            return (f"ctx-size = {trained} is this model's trained maximum; "
                    f"it caps there whether or not the file says so")
    if key in SERVER_DEFAULTS and SERVER_DEFAULTS[key] == value:
        return f"{key} = {_ini_value(value)} is llama-server's own default"
    return None


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
            lines.append(f"{key} = {_ini_value(body[key])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(personality: dict, budget_bytes: int | None = None) -> dict:
    """Generate presets.ini text for a personality, plus the budget it implies.

    Read-only. Returns the text, the diff against what is on disk, and the budget with
    its inputs. Writing is `apply()`'s job; this function never touches disk, and `apply()`
    validates the text this returns before it writes it.
    """
    name = personality.get("name")
    if not name:
        raise PersonalityError("personality needs a name")

    models_max = _models_max(personality)
    models = resolve_models(personality)
    overrides = personality.get("overrides") or {}

    # `global` sits on top of GLOBAL_DEFAULTS rather than replacing them, so an editor that
    # changes one key still emits the rest. GLOBAL_DEFAULTS is the safety net; the router's own
    # precedence is CLI > model section > [*], so what lands in [*] is the floor, not the ceiling.
    sections: dict[str, dict[str, Any]] = {"*": dict(GLOBAL_DEFAULTS)}
    glob = dict(personality.get("global") or {})
    hostile = sorted(k for k in glob if k.lower() in ROUTER_CONTROLLED_KEYS)
    if hostile:
        raise PersonalityError(
            f"personality {name!r} sets router-controlled key(s) {hostile} in [*]; "
            f"the router overwrites them"
        )
    sections["*"].update(glob)

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

    rows = {r["id"]: r for r in _rows()}
    budget = budget_bytes if budget_bytes is not None else modelindex._pool_bytes()

    # Over-capacity values are refusals, not warnings. presets.ini is read at load time by
    # something that will not explain itself, and both of these are ways to ask for more than
    # exists: a context the model was never trained for, and a context whose cache cannot be
    # paid for in this aperture.
    for section, per in sections.items():
        if section == "*" or per.get("ctx-size") is None:
            continue
        row = rows.get(section)
        if row is None:
            continue
        ctx = modelmeta._as_int(per["ctx-size"])
        trained = modelmeta._as_int(row.get("ctx_trained"))
        if ctx and trained and ctx > trained:
            raise PersonalityError(
                f"personality {name!r} asks ctx-size {ctx} for {section}; "
                f"the model is trained for {trained}")
        if ctx and budget:
            peak = modelindex.peak_bytes(row, ctx)
            if peak is None:
                peak = _conservative_peak(row, ctx)
            if peak is None:
                # Unpriced is not the same as free. Without attention geometry there is no
                # number to compare against the aperture, and a check that quietly passes when
                # it has nothing to measure is worse than no check — it reads as verification.
                raise PersonalityError(
                    f"personality {name!r} asks ctx-size {ctx} for {section}; its cache cannot be "
                    f"priced from the index, so it cannot be verified against a "
                    f"{budget / 2 ** 30:.1f} GiB aperture")
            if peak > budget:
                raise PersonalityError(
                    f"personality {name!r} asks ctx-size {ctx} for {section}: that cache costs "
                    f"{peak / 2 ** 30:.1f} GiB against a {budget / 2 ** 30:.1f} GiB aperture")

    # Then drop what would say nothing. Reported, not swallowed: an operator who set a value and
    # finds it absent from the file has to be able to learn why it is not there.
    omitted: list[dict] = []
    for section in list(sections):
        row = rows.get(section)
        for key in list(sections[section]):
            reason = _no_op_reason(key, sections[section][key], row)
            if reason:
                omitted.append({"section": section, "key": key,
                                "value": _ini_value(sections[section][key]), "reason": reason})
                del sections[section][key]
        if section != "*" and not sections[section]:
            # A section reduced to nothing is a header with no settings under it — the router
            # would list the model as carrying a custom preset that sets nothing.
            del sections[section]

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
        "omitted": omitted,
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


# ------------------------------------------------------------------- writing (N4 apply)
def _rollback(presets_store, stamp: str, expected: str, log) -> None:
    """Restore a snapshot and prove the file on disk matches it.

    A rollback that is only *attempted* is not a rollback. The failure this guards against is
    specific: the same I/O fault that truncated or corrupted the apply can also defeat the
    restore, and an error message that says "restored snapshot <stamp>" while the file is still
    broken is worse than no message at all — the operator stops reading and assumes the box is
    back to rights.

    If the restore itself failed, the snapshot is still on disk and the message says so with the
    exact command, because at that point the only truthful statement is where the good bytes are.
    """
    try:
        presets_store.restore(stamp)
    except presets_store.PresetsSnapshotError as exc:
        log(f"ROLLBACK FAILED: {exc}")
        raise PersonalityError(
            f"could not restore snapshot {stamp}: {exc}; the previous presets.ini is still "
            f"available at {presets_store.snapshot_dir(stamp) / presets_store.SNAPSHOT_NAME}"
        ) from exc

    restored = _current_text()
    if restored != expected:
        log(f"rollback left a file that does not match the snapshot ({len(restored)} bytes, "
            f"expected {len(expected)})")
        raise PersonalityError(
            f"rollback to snapshot {stamp} did not reproduce the previous file "
            f"({len(restored)} bytes on disk, {len(expected)} in the snapshot); "
            f"restore by hand from "
            f"{presets_store.snapshot_dir(stamp) / presets_store.SNAPSHOT_NAME}")
    log(f"rollback verified: {paths.PRESETS_INI} matches snapshot {stamp}")


def _reparse(text: str) -> dict[str, dict[str, str]]:
    """Parse generated text the way the router would read it, before it is on disk.

    This is the step that turns "the generator produced something" into "the generator produced
    something the router can load". It is deliberately a separate parse of the emitted string
    rather than a check of the `sections` dict: the thing that gets loaded is the file, so the
    thing that gets validated has to be the file.
    """
    cp = configparser.ConfigParser(allow_no_value=True, strict=True,
                                   comment_prefixes=("#", ";"))
    cp.read_string(text)
    return {s: dict(cp[s]) for s in cp.sections()}


def check_generated(text: str, known_models: set[str]) -> list[str]:
    """Every reason this text must not be written, as messages. [] means safe to write.

    Checked in the order that catches the likeliest mistakes first:
      * it must parse at all (the retired generator emitted a file configparser rejected);
      * no key the router strips or derives, and no key nobody has defined a meaning for;
      * every value must satisfy its registry `accepts()` after coercion, because ini values
        arrive as strings and `threads = 16` is not an int until something says so;
      * every section name must be `*` or a real library directory, because a section that
        matches nothing is a silent no-op that looks like a setting.
    """
    problems: list[str] = []
    try:
        sections = _reparse(text)
    except configparser.Error as exc:
        return [f"generated presets.ini does not parse: {exc}"]

    if not sections:
        return ["generated presets.ini has no sections"]

    for section, body in sections.items():
        where = "[*]" if section == "*" else f"[{section}]"
        if section != "*" and section not in known_models:
            problems.append(
                f"{where} is not a model in the library: the router matches sections to "
                f"directory names, so this section would be ignored")
        for key, value in body.items():
            if key in ROUTER_CONTROLLED_KEYS:
                problems.append(f"{where} {key}: the router overwrites this key at launch")
                continue
            if key in ROUTER_DERIVED_KEYS:
                problems.append(f"{where} {key}: the router derives this value itself")
                continue
            if key not in SETTINGS:
                problems.append(f"{where} {key}: no definition in the settings registry")
                continue
            try:
                coerced = coerce_setting(key, value)
            except PersonalityError as exc:
                problems.append(f"{where} {key}: {exc}")
                continue
            reason = check_setting(key, coerced)
            if reason:
                problems.append(f"{where} {key} = {value}: {reason}")
    return problems


def normalize_proposed(base: dict, global_edits: dict | None,
                       model_edits: dict | None) -> dict:
    """Turn an editor payload into a personality dict, refusing anything the registry rejects.

    This is the boundary between "a value in a form field" and "a value the generator may emit",
    and it does three things the generator alone does not:

      * coerces first. A form submits strings and ini values are strings too, so `threads = 16`
        arrives at `accepts()` as `"16"`; a validator that demands an int rejected every value the
        live router itself reports.
      * refuses a key with no definition, a key the router strips, and a key the router derives —
        each with its own reason, because a form that silently drops a field is how an operator
        ends up believing a setting they can see is a setting that exists.
      * collects every problem instead of stopping at the first, so a form is not cleared one
        field at a time.

    Returns a personality dict carrying validated `global` and `overrides`. It never writes.
    """
    errors: list[str] = []
    out = dict(base)

    def clean(where: str, raw: dict | None) -> dict:
        cleaned: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            if key in ROUTER_CONTROLLED_KEYS:
                errors.append(f"{where} {key}: the router overwrites this key at launch")
                continue
            if key in ROUTER_DERIVED_KEYS:
                errors.append(f"{where} {key}: the router derives this value itself")
                continue
            if key not in SETTINGS:
                errors.append(f"{where} {key}: no definition in the settings registry")
                continue
            try:
                coerced = coerce_setting(key, value)
            except PersonalityError as exc:
                errors.append(f"{where} {key}: {exc}")
                continue
            reason = check_setting(key, coerced)
            if reason:
                errors.append(f"{where} {key}: {reason}")
                continue
            cleaned[key] = coerced
        return cleaned

    glob = clean("[*]", global_edits)

    # A per-model edit has to land on a model this personality actually covers. render() builds
    # sections by iterating resolve_models(), so an override for any other name is dropped on the
    # floor: the editor would show the value, preview would answer 200, and the generated file
    # would not contain it. That is the silent no-op this module keeps running into, so the two
    # ways it happens are named separately rather than merged into one vague refusal.
    covered: list[str] = []
    try:
        covered = resolve_models(out)
    except PersonalityError as exc:
        errors.append(str(exc))
    library = {r["id"] for r in _rows()}

    per_model: dict[str, dict[str, Any]] = {}
    for mid, raw in (model_edits or {}).items():
        cleaned = clean(f"[{mid}]", raw)
        if raw and not cleaned:
            per_model[mid] = cleaned      # every field was refused; the refusal is already logged
            continue
        if mid not in library:
            errors.append(f"[{mid}]: no library entry, so the router would ignore this section")
            continue
        if mid not in covered:
            errors.append(f"[{mid}]: not part of personality {out.get('name')!r}'s model set, "
                          f"so this edit would be discarded rather than written")
            continue
        per_model[mid] = cleaned

    if errors:
        raise PersonalityError("the proposed settings were refused:\n  "
                               + "\n  ".join(errors))

    if glob:
        out["global"] = glob
    merged = dict(out.get("overrides") or {})
    for mid, body in per_model.items():
        if body:
            merged[mid] = body
    if merged:
        out["overrides"] = merged
    return out


def apply(personality: dict, budget_bytes: int | None = None,
          log=print, restart: bool = True) -> dict:
    """Write presets.ini for a personality: check, snapshot, write, verify, restart.

    Order is the safety property, and every step is a refusal point:

      1. render() the text (raises on router-controlled overrides);
      2. check_generated() re-parses the emitted text and validates every key and value against
         SETTINGS and the live library — a file that fails here never touches disk;
      3. snapshot the file that is currently on disk, read from disk, before replacing it;
      4. write, then read the file back and re-check it: if what landed on disk is not what was
         validated, restore the snapshot and raise. A partial or truncated write is the failure
         mode that would otherwise be discovered by the router at load time;
      5. restart the router only if the file on disk verified.

    If the budget does not fit, this refuses rather than writing, because the portal already
    priced the heaviest combination this personality can load and writing a plan that provably
    does not fit would just relocate the failure to load time. `fit` is not the backstop it once
    was claimed to be: §6.12.2 measured it blind to memory other processes hold and unable to
    bound the model it is applied to, which is why it is no longer emitted.
    """
    from . import presets_store

    rendered = render(personality, budget_bytes)
    budget = rendered["budget"]

    if budget["fits"] is FITS_NO:
        raise PersonalityError(
            f"refusing to write personality {rendered['name']!r}: its heaviest "
            f"{rendered['models_max']}-model combination costs "
            f"{_gib(budget['worst_case_bytes'])} against a "
            f"{_gib(budget['budget_bytes'])} budget"
        )
    if budget["fits"] is FITS_UNKNOWN:
        raise PersonalityError(
            f"refusing to write personality {rendered['name']!r}: its cost cannot be priced "
            f"({', '.join(budget['unpriced']) or 'no memory budget readable'}); an unpriced "
            f"plan is not a plan"
        )

    known = {r["id"] for r in _rows()}
    problems = check_generated(rendered["text"], known)
    if problems:
        raise PersonalityError(
            f"refusing to write personality {rendered['name']!r}:\n  "
            + "\n  ".join(problems))

    snap = presets_store.snapshot(reason=f"apply personality {rendered['name']}",
                                  personality=rendered["name"])
    log(f"snapshotted presets.ini -> {snap['path']}")

    before = _current_text()
    try:
        paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
        paths.PRESETS_INI.write_text(rendered["text"])
    except OSError as exc:
        raise PersonalityError(f"could not write {paths.PRESETS_INI}: {exc}") from exc

    # Verify what is actually on disk, not what the write call reported.
    landed = _current_text()
    if landed != rendered["text"]:
        _rollback(presets_store, snap["stamp"], before, log)
        raise PersonalityError(
            f"{paths.PRESETS_INI} does not match what was validated "
            f"({len(landed)} bytes written, {len(rendered['text'])} validated); "
            f"restored snapshot {snap['stamp']}")
    post = check_generated(landed, known)
    if post:
        _rollback(presets_store, snap["stamp"], before, log)
        raise PersonalityError(
            f"{paths.PRESETS_INI} failed validation after writing:\n  "
            + "\n  ".join(post) + f"\n  restored snapshot {snap['stamp']}")

    db.event("portal", "presets-apply",
             f"personality={rendered['name']} models={len(rendered['models'])} "
             f"models_max={rendered['models_max']} snapshot={snap['stamp']}")
    log(f"wrote {paths.PRESETS_INI} ({len(landed)} bytes) for personality "
        f"{rendered['name']}")
    presets_store.prune()

    result = {
        "name": rendered["name"],
        "models": rendered["models"],
        "models_max": rendered["models_max"],
        "snapshot": snap["stamp"],
        "bytes": len(landed),
        "changed": before != rendered["text"],
        "budget": budget,
        "restart": None,
    }
    if not restart:
        return result

    from . import systemd
    state = systemd.service_action("llamacpp", "restart")
    result["restart"] = state
    if not state.get("ok"):
        # The file is valid and the snapshot exists. Rolling back automatically here would be a
        # guess: the restart may have failed for a reason nothing to do with presets.ini, and
        # silently reverting a config the operator just asked for is worse than telling them the
        # restart failed and what to roll back to.
        log(f"router restart FAILED: {state.get('error')}; presets.ini is written and "
            f"snapshot {snap['stamp']} can restore the previous file")
    else:
        log(f"restarted {paths.ROUTER_UNIT}")
    return result


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
