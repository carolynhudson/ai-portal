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
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core import db, model_config, modelindex, modelmeta, paths

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
    # Added when the settings registry grew these keys, so the no-op rule covers everything the
    # editor can actually write rather than everything remembered at the time. All read from b10964:
    # `--fit` default 'on', `--fit-target` 1024, `--keep` 0, `--cache-ram` 8192, `--checkpoint-min-step`
    # 8192, `cache_idle_slots = true` in common/common.h.
    #
    # `fit = on` is the historical example this table exists for: the file on this box used to carry
    # that line, and it said nothing — the server already does it. See §6.12.7 for what fit actually
    # does, which is not what this portal previously recorded.
    "fit": "on",
    "fit-target": 1024,
    "keep": 0,
    "cache-ram": 8192,
    "checkpoint-min-step": 8192,
    "cache-idle-slots": True,
    # Measured on this box (§4.10.2): a router whose preset sets nothing produced zero startup
    # loads, and a preset saying `load-on-startup = off` produced zero startup loads. Identical, so
    # the false *is* the unset state and writing it is noise. `on` is the decision -- and it is only
    # safe in a model section, which is why render() refuses it anywhere else.
    "load-on-startup": False,
    # Added 2026-10-06 by tools/registry_defaults_audit.py, which flags any key the editor can
    # write that has no entry here: without one, nothing is ever recognised as the default, so a
    # member of the preset-only trio written as `off` lands in the file as a line that changes
    # nothing (§4.10.0). The router hides nothing by default (`dedup-cache-models` is read from
    # the preset in server-models.cpp; absent means do not hide), so false is the unset state.
    "dedup-cache-models": False,
    # Added 2026-10-04, read from the binary this box runs (b11396 --help), for the keys the
    # registry gained with §6.12.20. Two things to know before trusting these numbers: they are
    # b11396's, and the block above is b10964's, so this table now spans two builds; and the
    # sampling defaults are the reason the sampling keys are worth having -- a model recommended
    # at temperature 1.0 is running the server's 0.80 unless somebody says otherwise.
    # Read from b11429 --help (§6.12.26): `--spec-type` defaults to none, so writing none is a
    # restatement; `--spec-draft-sampling` defaults to greedy for the same reason.
    "spec-type": "none",
    "spec-draft-sampling": "greedy",
    "kv-unified": "auto",          # false in common.h:578, enabled when slots are auto
    "kv-unified-per-slot": 0,      # common.h:636
    "fit-ctx": 4096,               # common.h:483
    "temp": 0.80,
    "top-k": 40,
    "top-p": 0.95,
    "min-p": 0.05,
    "presence-penalty": 0.00,
    "repeat-penalty": 1.00,
    "dry-multiplier": 0.00,
}

#: Deliberately absent: `dedup-cache-models`. Its behaviour lives in the router's preset reader
#: rather than in the argument table, and it was not measured here; an unverified default in this
#: table would suppress a line that is not a no-op.
#:


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
#: The speculative-decoding types b11429 accepts, quoted from its own --help. Named once: the
#: validator and the row's allowed-values list read the same tuple, so a key cannot accept what
#: its own help says it does not.
_SPEC_TYPES = (
    "none", "draft-simple", "draft-eagle3", "draft-mtp", "draft-dflash", "draft-dspark",
    "ngram-simple", "ngram-map-k", "ngram-map-k4v", "ngram-mod", "ngram-cache",
)

SETTINGS: dict[str, dict[str, Any]] = {
    # The host-RAM knobs. They were absent from the registry entirely, which meant an operator
    # could not set them and the portal's budget did not account for them -- while llama.cpp runs
    # them by default at 8 GiB of prompt cache and 32 checkpoint copies per slot. On this box that
    # is the difference between "fits the aperture" and "fits the machine": 108 GiB of GTT carved
    # out of 122.8 GiB of RAM, and the caches live in the RAM, not the aperture.
    "cache-ram": {
        "kind": "int",
        "section": "both",
        "default": 8192,
        "help": "Prompt cache limit, in MiB of system RAM. Default 8192. 0 disables it. This is not GPU memory.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.6",
        "side_effects": "Charged against host RAM, which on this box is the same memory the "
                        "aperture is carved from. Raising it does not spend GPU memory, it spends "
                        "the machine.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "ctx-checkpoints": {
        "kind": "int",
        "section": "both",
        "default": 32,
        "help": "Copies of a slot's cache, kept in system RAM. 32 per slot by default. Each costs 19-50 MiB here.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.6",
        "side_effects": "Cost is per slot and constant -- it does not scale with context size or "
                        "with how long a session runs, and it lands in host RAM, not the aperture "
                        "(§6.12.6).",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "checkpoint-min-step": {
        "kind": "int",
        "section": "both",
        "default": 8192,
        "help": "Minimum tokens between cache checkpoints. Changes how often they are made, not how big each is.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.6",
        "side_effects": "Lower spacing means more checkpoints alive at once. The multiplier is this "
                        "number, not the context length.",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "cache-idle-slots": {
        "kind": "bool",
        "section": "both",
        "default": True,
        "help": "Save an idle slot's state when a new task starts. Costs system RAM, and needs cache-ram set.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.6",
        "side_effects": "This is what lets a finished conversation keep holding RAM. Leaving it on "
                        "is the reason an idle model can cost more than an active one.",
        "accepts": lambda v: isinstance(v, bool),
    },
    "threads": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "CPU threads for the prompt pass. Threads 1 is the fastest value measured here, so set it on purpose.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.6",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "fit": {
        "kind": "choice",
        "section": "both",
        "default": "on",
        "choices": ("on", "off"),
        "help": "Shrink unset settings so the model fits the device. It reads what other programs hold. It can cut your context.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.7",
        "side_effects": "May silently choose a smaller context than the model can address. A load "
                        "can satisfy fit's target and still fail, because fit's projection omits the "
                        "compute graph allocated after it (§6.12.7).",
        "accepts": lambda v: v in ("on", "off"),
    },
    "kv-unified": {
        "kind": "choice",
        "section": "both",
        "default": "auto",
        "choices": ("on", "off", "auto"),
        # The preset parser maps the value to a flag; the flag itself takes no value.
        # Measured on the CLI: `--kv-unified auto` is refused, `--kv-unified` is accepted.
        # So this key is probed through a preset, never on a command line.
        "preset_mapped": True,
        # What the build states as its own default, recorded because it is NOT this key's
        # default: b11429's help says 'enabled'. The portal keeps 'auto' as a sentinel meaning
        # "no opinion, omit the line", which reproduces the server's behaviour because auto
        # measures as on (§6.12.21). tools/registry_defaults_audit.py reports this row as a
        # documented deviation instead of drift, so the next build that changes the real default
        # is visible rather than absorbed.
        "server_default": "enabled",
        "help": "Shares one cache pool across slots. On: a client can use the whole pool. Off: "
                "it gets one slice. Auto also turns it on — measured, not server-decided. To let "
                "the server decide, remove this key from the preset.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.21, llama-cpp.cpp:353-361",
        "side_effects": "Decides whether ctx-size is a window each slot can reach or a total to "
                        "be divided: unified, window = pool; sliced, window = pool / slots. The "
                        "measured difference on the same file and the same ctx-size is 32768 "
                        "versus 16384 (§6.12.20). In a preset the server rewrites the value: on, "
                        "true and auto all reach the worker as the bare --kv-unified; off reaches "
                        "it as --no-kv-unified (§6.12.21).",
        "accepts": lambda v: v in ("on", "off", "auto"),
    },
    "kv-unified-per-slot": {
        "kind": "int",
        "section": "both",
        "default": 0,
        "help": "Tokens each slot may use. With ctx-size unset, the pool is slots x this number.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.15",
        "side_effects": "It caps a slot below the model's trained context by design -- that is "
                        "the point, and the cost: KV paid per slot caps each client, and a value "
                        "above the pool's per-slot share is reported as having no effect. Set "
                        "ctx-size alongside it to price the pool you mean (§6.12.20).",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "fit-ctx": {
        "kind": "int",
        "section": "both",
        "default": 4096,
        "help": "The smallest context fit may shrink a slot to. Works only when ctx-size is unset.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.7",
        "side_effects": "It is the floor of a reduction nobody sees in a normal log: with "
                        "ctx-size unset and the aperture short, fit interpolates the pool down "
                        "toward slots x this number and logs it at trace level only. Lower it and "
                        "a tight box can be allowed to shrink a model further (§6.12.20).",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "fit-target": {
        "kind": "int",
        "section": "both",
        "default": 1024,
        "help": "Margin in MiB the fit adjustment leaves per device. Comma-separated for "
                "a per-device list; one value applies to every device.",        "cite": "docs/PORTAL_PLAN.md sec 6.12.7",

        "side_effects": "Raising it makes fit shrink context sooner. It reserves aperture, not host "
                        "RAM -- and the prompt cache and checkpoints live in host RAM, so a generous "
                        "target does not prevent the kills this box actually gets.",
        "accepts": lambda v: _positive_mib_list(v),
    },
    "stop-timeout": {
        "kind": "int",
        "section": "both",
        "default": 10,
        "preset_only": True,
        "help": "Seconds to wait after a requested unload before forcing termination. "
                "Preset-exclusive: it is not a command-line argument at all.",        "cite": "tools/server/server-models.cpp:43 (router-only key)",

        "side_effects": "A deadline that ends in a force-kill: unloading at the timeout stops a "
                        "generation in flight rather than waiting for it (§6.12.2).",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "load-on-startup": {
        "kind": "bool",
        "section": "model",
        "default": False,
        "preset_only": True,
        "help": "Load this model when the server starts, not on the first request. Read at startup only.",
        "cite": "tools/server/server-models.cpp:43 (router-only key)",
        "side_effects": "Decides what is resident before the first request. Startup loads are not "
                        "capped by models-max -- measured, two began with models-max = 1 in the same "
                        "file. It belongs in a model's own section: in [*] it reaches cached models "
                        "the personality never named (§4.10.2).",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "dedup-cache-models": {
        "kind": "bool",
        "section": "both",
        "default": False,
        "preset_only": True,
        "help": "When a preset's hf-repo points at a model already downloaded, hide the "
                "cached entry from GET /models. Preset-exclusive.",        "cite": "tools/server/server-models.cpp (router-only key)",

        "side_effects": "Only the listing changes: the model stays on disk and still resolvable.",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "ctx-size": {
        "kind": "int",
        "section": "model",
        "default": 0,
        "help": "Total tokens the server shares among its slots. A client gets this divided by the slot count.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.20",
        "side_effects": "Two opposite silences, in both directions (§6.12.20). UNSET: fit sizes "
                        "the pool to slots x trained when it can afford it and interpolates it "
                        "DOWN when it cannot, logging the reduction at trace level only -- that "
                        "is the report of 'the router says this model has half the context it "
                        "used to report', with no setting set. SET: fit is locked out "
                        "(arg.cpp:1643-1645) and this becomes the model's ceiling; nothing "
                        "raises it back, and a value below the trained context removes "
                        "capability quietly (§4.10.1).",
        "accepts": lambda v: isinstance(v, int) and v >= 0,
    },
    "n-gpu-layers": {
        "kind": "choice-or-int",
        "section": "both",
        "default": "auto",
        "help": "Layers loaded to the GPU: 99 loads all of them. Lower numbers move work to system RAM.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.18",
        "side_effects": "Weight layers moved out of the aperture become anonymous, unreclaimable RAM "
                        "that cannot be paged out -- the signature every OOM kill in this box's "
                        "journal carries (§6.12).",
        "accepts": lambda v: v in ("auto", "all") or (isinstance(v, int) and v >= 0),
    },
    "flash-attn": {
        "kind": "choice",
        "section": "both",
        "default": "auto",
        "choices": ("on", "off", "auto"),
        "help": "Speeds up attention. Needs v4: on a v3 GPU it makes output worse, not faster.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.19",
        "side_effects": "A performance lever with a capacity side effect: it changes the attention "
                        "path a long context is allocated through.",
        "accepts": lambda v: v in ("on", "off", "auto"),
    },
    "parallel": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "Slot count. Each slot gets its own window, so 2 slots halves what one client gets.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.20, server.cpp:161-166",
        "side_effects": "THIS SETTING DIVIDES THE CONTEXT, and only when you set it. Naming a "
                        "slot count turns the unified pool off (kv_unified defaults false), and "
                        "ctx-size is then shared out: ctx-size 32768 at 2 slots is a 16384-token "
                        "window per client, with llama.cpp logging 'the full capacity of the model "
                        "will not be utilized'. It also multiplies the per-slot caches (§6.12.6). "
                        "Leave it auto, or set kv-unified with it (§6.12.20).",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    # A model's upstream "recommended llama settings" are made of these. They are per-model
    # facts about how the weights were sampled, which is why a personality must be able to carry
    # them: a file that was trained to be used at temperature 1.0 is not well served by a router
    # running the server's default 0.80, and a personality that cannot say so is not a
    # personality. Eligibility measured on b11396 (§6.12.20): all of these are accepted in a
    # preset section.
    "temp": {
        "kind": "float",
        "section": "both",
        "default": 0.80,
        "help": "0.0 repeats the same answer. 1.0 wanders. 0.7 sits between.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",
        "side_effects": "Quality, not capacity. Nothing here limits context.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 2.0,
    },
    "top-k": {
        "kind": "int",
        "section": "both",
        "default": 40,
        "help": "Pick from the N most likely next words. 40 is the default; 0 means no limit.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",
        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    },
    "top-p": {
        "kind": "float",
        "section": "both",
        "default": 0.95,
        "help": "Sample from the smallest set of tokens whose cumulative probability reaches p; "
                "1.0 disables.",        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",

        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0,
    },
    "min-p": {
        "kind": "float",
        "section": "both",
        "default": 0.05,
        "help": "Drop answers below this odds. 0.05 keeps more words than 0.1.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",
        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0,
    },
    "presence-penalty": {
        "kind": "float",
        "section": "both",
        "default": 0.00,
        "help": "Penalty on tokens already present in the context.",        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",

        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    },
    "repeat-penalty": {
        "kind": "float",
        "section": "both",
        "default": 1.00,
        "help": "Push down words you just used. 1.0 is off.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",
        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
    },
    "dry-multiplier": {
        "kind": "float",
        "section": "both",
        "default": 0.00,
        "help": "Penalise phrases you repeat. 1.0 is off. 1.5 stops loops sooner.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.5",
        "side_effects": "Quality, not capacity.",
        "accepts": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
    },
    "cache-prompt": {
        "kind": "bool",
        "section": "both",
        "default": True,
        "help": "Keep a finished slot's state so a similar request answers faster. Paid in host RAM after the request ends.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.19",
        "side_effects": "The speed it buys is paid for in host RAM held after the request ends.",
        "accepts": lambda v: isinstance(v, bool),
    },
    "reasoning-budget": {
        "kind": "int",
        "section": "both",
        "default": -1,
        "help": "Tokens the model may spend on private reasoning. -1 means no limit. The server rejects 0.",
        # Declared twice before 2026-10-06: Python kept the second one, so the citation the page
        # linked was always the server.cpp one and the bare 4.10.3 line was dead text.
        "cite": "docs/PORTAL_PLAN.md sec 4.10.3, server.cpp:360-363",
        "side_effects": "Session cost, not load cost: it cannot make a model cheaper to bring up.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "reasoning-effort": {
        "kind": "choice",
        "section": "both",
        "default": "default",
        "choices": ("default", "minimal", "low", "medium", "high", "xhigh", "max"),
        "help": "Reasoning level passed to the chat template. Qwen3.8-Flash-Next obeys this one, not reasoning-budget.",
        "cite": "docs/PORTAL_PLAN.md sec 4.10.3",
        "side_effects": "Session cost, not load cost -- and only honoured at all if the model's "
                        "template reads the field.",
        "accepts": lambda v: v in ("default", "minimal", "low", "medium", "high", "xhigh", "max"),
    },
    "jinja": {
        "kind": "bool",
        "section": "both",
        "default": True,
        "help": "Serve the model's own chat template, with tool calling. Off, tool calls stop working.",
        "cite": "docs/PORTAL_PLAN.md sec 4.10.5",
        "side_effects": "Off removes the template's role and tool handling: the tokens are still in "
                        "the prompt, nothing interprets them.",
        "accepts": lambda v: isinstance(v, bool) or v in (0, 1),
    },
    "keep": {
        "kind": "int",
        "section": "both",
        "default": 0,
        "help": "Keep the last N tokens of a conversation. 0 ends a session when it runs out of room.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.19",
        "side_effects": "Pinned tokens are charged against the slot's context for the whole "
                        "session, not for the turns that used them.",
        "accepts": lambda v: isinstance(v, int) and v >= -1,
    },
    "lazy-mode": {
        "kind": "choice",
        "section": "both",
        "default": "auto",
        "choices": ("on", "off", "auto"),
        "help": "Read big tensors from disk when needed, not from memory. auto applies above 4 GiB. on applies always.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.18",
        "side_effects": "Measured on this box: 80.5 GiB of aperture with lazy-mode unset against "
                        "54.5 GiB with it on, same model and build -- weights stay in reclaimable "
                        "page cache instead of pinned buffers. A kernel upgrade landed at the same "
                        "time, so that gap is not attributable to this flag alone.",
        "accepts": lambda v: v in ("on", "off", "auto"),
    },
    # --- speculative decoding ---------------------------------------------------------
    # Added 2026-10-06, and deliberately as the test case for §6.14.1's claim that a new setting
    # is a data edit. Both flags are in b11429's own `--help`; both defaults are quoted from that
    # help; and §6.12.17 measured that a preset section really carries `spec-type` down to the
    # worker's command line, which is what makes them portal settings at all (§6.12.11 recorded
    # the opposite belief, from a CLI-only reading). Acceptance measured with
    # tools/registry_flag_audit.py and recorded in tests/test_personality.py::AUDITED_B11429_ACCEPTS.
    "spec-type": {
        "kind": "list",
        "section": "both",
        "default": "none",
        "values": _SPEC_TYPES,
        "help": "Speculative decoding to run: a draft guesses the next tokens and the target verifies them. Comma-separated.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.11, sec 6.12.17",
        "side_effects": "A draft file costs aperture alongside the target. A file carrying its own head (the index's nextn_layers, §4.9.3) needs no second model; the 177B daily model on this box does not carry one, so a draft spec for it buys speed with a second resident file.",
        "accepts": lambda v: isinstance(v, str) and all(
            p.strip() in _SPEC_TYPES for p in v.split(",") if p.strip()),
    },
    "spec-draft-sampling": {
        "kind": "choice",
        "section": "both",
        "default": "greedy",
        "choices": ("greedy", "probabilistic"),
        "help": "How the draft's guess is made: greedy takes its argmax, probabilistic samples it and lets the target verify by rejection sampling.",
        "cite": "docs/PORTAL_PLAN.md sec 6.12.26",
        "side_effects": "Changes how many guesses are accepted, not what the draft costs. Meaningless with no speculative decoding running at all -- it is a knob on a draft, not a way to start one.",
        "accepts": lambda v: v in ("greedy", "probabilistic"),
    },
}

# ---------------------------------------------------------------------------
# Presentation data (docs/PORTAL_PLAN.md §6.14.1)
#
# Everything above is what a key *means*: the validator, its kind, its default, the sentence the
# page shows. Everything in this table is what a *renderer* needs on top of that, and it is merged
# into SETTINGS below so there is one table to read and not two to keep in step.
#
#   aliases        the spellings b11429's own --help declares for the key. Measured with
#                  tools/registry_defaults_audit.py (§6.12.26), not remembered: `-ctxcp` declares
#                  `--ctx-checkpoints` and `--swa-checkpoints`, `-ngl` declares `--gpu-layers` too,
#                  and the preset-only trio declares nothing because it has no CLI form.
#   range          the bounds this portal enforces, read off its own `accepts` rule. Not stated
#                  where the portal enforces no bound, because an invented bound is a rule that
#                  disagrees with the validator -- tests/test_settingrows.py probes each bound
#                  against `accepts`, one step outside the lower bound included.
#   specials       words a numeric field takes that are not numbers.
#   unit           what a number counts, so the page can say 8192 MiB and not 8192.
#   visible_when   model-index facts (§4.9) that must be true for the row to mean anything, read
#                  from the index at render time -- see app/core/settingrows.py.
#   substitutions  measured facts the help text quotes, resolved by settingrows.substitutions().
# ---------------------------------------------------------------------------
SETTING_PRESENTATION: dict[str, dict[str, Any]] = {
    "cache-ram": {"aliases": ("-cram",), "unit": "MiB", "range": (-1, None),
                  "substitutions": ("cache_ram_mib",)},
    "ctx-checkpoints": {"aliases": ("-ctxcp", "--swa-checkpoints"), "range": (0, None),
                        # §6.12.6: checkpoints are gated per model on sliding-window / bounded
                        # seq-removal, so the row is noise on a model that reports none.
                        "visible_when": ("sliding_window",),
                        "substitutions": ("checkpoint_mib",)},
    "checkpoint-min-step": {"aliases": ("-cms",), "unit": "tokens", "range": (0, None),
                            "visible_when": ("sliding_window",)},
    "cache-idle-slots": {"aliases": ("--no-cache-idle-slots",),
                         "substitutions": ("cache_ram_mib",)},
    "threads": {"aliases": ("-t",), "range": (-1, None), "substitutions": ("threads_default",)},
    "fit": {"aliases": ("-fit",)},
    "kv-unified": {"aliases": ("-kvu", "--no-kv-unified")},
    "kv-unified-per-slot": {"range": (0, None)},
    "fit-ctx": {"aliases": ("-fitc",), "unit": "tokens", "range": (0, None)},
    "fit-target": {"aliases": ("-fitt",), "unit": "MiB"},
    "stop-timeout": {"unit": "seconds", "range": (0, None)},
    "load-on-startup": {},
    "dedup-cache-models": {},
    "ctx-size": {"aliases": ("-c",), "unit": "tokens", "range": (0, None),
                 "substitutions": ("ctx_trained",)},
    # Range read off this portal's own validator (`v in ("auto", "all") or int >= 0`), not off
    # llama.cpp's CLI where -1 also means auto. Advertising -1 here would advertise a value the
    # registry's `accepts` refuses -- which is what tests/test_settingrows.py caught when this
    # row first said -1.
    "n-gpu-layers": {"aliases": ("-ngl", "--gpu-layers"), "specials": ("auto", "all"),
                     "range": (0, None)},
    "flash-attn": {"aliases": ("-fa",)},
    "parallel": {"aliases": ("-np",), "range": (-1, None)},
    "temp": {"range": (0.0, 2.0)},
    "top-k": {"range": (0, None), "unit": "tokens"},
    "top-p": {"range": (0.0, 1.0)},
    "min-p": {"range": (0.0, 1.0)},
    "presence-penalty": {},
    "repeat-penalty": {},
    "dry-multiplier": {"range": (0.0, None)},
    "cache-prompt": {"aliases": ("--no-cache-prompt",)},
    "reasoning-budget": {"unit": "tokens", "range": (-1, None)},
    "reasoning-effort": {},
    "jinja": {"aliases": ("--no-jinja",),
              # A template switch on a file with no chat template has nothing to switch.
              "visible_when": ("chat_template",),
              # What the control offers, in the order it should offer it. `on`/`off` lead because
              # that is what the portal writes (`_ini_value`); the router reports this key as
              # "enabled", which is a value, not the only spelling that means it.
              "accepted": ("on", "off", "enabled", "disabled", "true", "false", "1", "0")},
    "keep": {"range": (-1, None), "unit": "tokens"},
    "lazy-mode": {"aliases": ("-lzm",)},
    "spec-type": {"substitutions": ("spec_types",)},
    "spec-draft-sampling": {},
}

for _key, _extra in SETTING_PRESENTATION.items():
    if _key not in SETTINGS:
        raise RuntimeError(
            f"SETTING_PRESENTATION names {_key!r}, which is not a setting: the presentation table "
            f"cannot describe a key the registry does not define, because the page would offer a "
            f"row for a key nothing validates.")
    SETTINGS[_key].update(_extra)

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


#: The spellings a preset boolean can carry, transcribed from the runtime's own rule —
#: `is_truthy`/`is_falsey` in `common/arg.cpp` at v0.6.0 — and re-checked by
#: tests/test_registry_defaults_audit.py against every boolean key's help line in that build's
#: `--help`. A preset value arrives as text and becomes a bool through `is_truthy`
#: (`common/preset.cpp:155`), so these words — and **not** `yes`/`no`, which it does not know — are
#: the complete set. The form's control and the validator read this list, so a control cannot
#: offer a spelling the router would ignore.
TRUTHY = ("on", "enabled", "true", "1")
FALSEY = ("off", "disabled", "false", "0")

#: The words a boolean control offers, in offer order: truthy first, then falsey, so a select
#: reads "on, enabled, true, 1, off, disabled, false, 0" rather than shuffling the runtime's
#: vocabulary. A key may override this list (`accepted`); it may not extend it.
BOOL_SPELLINGS = TRUTHY + FALSEY


def coerce_setting(key: str, value: Any) -> Any:
    """Turn an ini/form value into the type this key's definition expects.

    `presets.ini` has no types: everything arrives as a string, so `threads = 16` reaches
    a validator as '16'. Rejecting that — which the first version of this function did,
    failing 34 out of 34 values the live router itself reports — is not strictness, it is
    the validator disagreeing with the file format it validates. The coercion is where the
    string becomes an int, and it is deliberately not permissive about the rest:
    '16.5' and 'sixteen' both stay strings and are then rejected.

    Booleans and floats are coerced from **the text the runtime accepts**, not from English:
    `TRUTHY`/`FALSEY` are `is_truthy`/`is_falsey`'s own words, and a float is a decimal string
    because the preset path calls `std::stof` (`common/arg.cpp:2021`, `--temp`).

    This paragraph used to read "Booleans are coerced from 1/0", describing a conversion that does
    not happen: `presets.ini` and the form both deliver text, so `'1'` stayed a string, `accepts`
    refused it, and **every boolean and float key in the registry was unwritable from the page** —
    11 of 32 keys, `jinja`, `temp`, `top-p`, `min-p`, `presence-penalty` and `repeat-penalty`
    among them, which is to say the sampling settings a model card recommends. The API contract is
    `settings: dict[str, str]` (`app/main.py`), so there is no code path that ever sends a Python
    number; the tests that exercised settings used `ctx-size` and `parallel`, which coerce, and the
    gap stayed shut.
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
    if kind == "bool":
        # Lowercased: `is_truthy` compares exact words and the portal writes lowercase.
        low = text.lower()
        if low in TRUTHY:
            return True
        if low in FALSEY:
            return False
        return text  # 'yes', 'no', 'nope' — refused by name, not guessed into a bool
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return value  # 'high', '0.' — left as text so the refusal names what was typed
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

#: `safe` — the thing to fall back to. Minimal on purpose: one model, nothing else
#: resident. If this one cannot start, the box is broken and the portal says so.
#:
#: It names a hub-cache row, not a library directory, because the library is empty on this box and
#: the fallback has to be something that is actually here. That is a real dependency: prune the
#: cache and this personality refuses to render — with the candidates listed beside the refusal —
#: rather than writing a preset for weights that are gone. Chosen among the *chat-capable* cached
#: files rather than the smallest cached files: the two smallest are an embedder (0.31 GiB) and a
#: reranker (0.60 GiB), and neither can answer the completion a fallback exists to answer.
SAFE = {
    "name": "safe",
    "models_max": 1,
    "note": "fallback state; one small chat model, nothing else resident",
    "models": ["Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0"],
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
    # A second, independent ceiling. `fits` answers "does the loaded set fit the GPU aperture";
    # these answer "does the loaded set plus its host-RAM caches fit the machine". They can disagree.
    host_cache_bytes: int | None = None
    host_notes: list[str] = field(default_factory=list)
    ram_total_bytes: int | None = None
    ram_headroom_bytes: int | None = None
    fits_ram: bool | None = FITS_UNKNOWN

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
            "host_cache_bytes": self.host_cache_bytes,
            "host_notes": self.host_notes,
            "ram_total_bytes": self.ram_total_bytes,
            "ram_headroom_bytes": self.ram_headroom_bytes,
            "fits_ram": self.fits_ram,
        }


def _rows() -> list[dict]:
    """Library rows from the index. Never rescan here: personality generation happens
    on a request path, and a scan of the model tree does not belong on one."""
    return modelindex.rows()


def resolve_targets(personality: dict) -> list[dict]:
    """Expand a personality's `models` into (library model, model config) pairs.

    An entry is one of two things. A library model id means that file under its own name with
    llama.cpp's own defaults and no config at all — which is what `default` and `safe` are made of,
    and why they cannot be changed by accident. The name of a stored model config means the same
    file plus that config's settings, addressed by the config's name (§4.10.4).

    Two rules here are refusals rather than conveniences:

      * an unknown name raises, because a personality that quietly drops a model the operator named
        is a personality the operator will trust until the model is missing;
      * two entries covering the same file raise, because a preset has one section per model, so
        "which config won" would be answered by dictionary ordering.
    """
    known = {r["id"] for r in _rows()}
    configs = {c["name"]: c for c in model_config.rows()}
    named = personality.get("models")
    if named is None:
        return [{"model": mid, "config": None} for mid in sorted(known)]

    out: list[dict] = []
    unknown: list[str] = []
    covered: dict[str, str] = {}
    for entry in named:
        if entry in known:
            base, conf = entry, None
        elif entry in configs:
            base, conf = configs[entry]["base_model"], entry
        else:
            unknown.append(entry)
            continue
        if base in covered:
            raise PersonalityError(
                f"personality {personality.get('name')!r} assigns {covered[base]!r} and "
                f"{entry!r} to the same model {base!r}; a preset has one section per model, so "
                f"which config's settings and whose name win would be decided by ordering")
        covered[base] = entry
        out.append({"model": base, "config": conf})

    if unknown:
        hints = "; ".join(f"{m}{_availability(m)}" for m in unknown)
        raise PersonalityError(
            f"personality {personality.get('name')!r} names models or model configs with no "
            f"library entry and no stored config: {hints}"
        )
    return out


def suggest_for(missing: str, rows: list[dict] | None = None,
                limit: int = 3) -> dict[str, list[dict]]:
    """What the operator could use instead, when a personality names something absent.

    A refusal that only says "no such model" sends the operator to another page to find out what IS
    here, and the honest answer to "where did Qwen3.5-0.8B go" is usually "the library is empty now;
    everything lives in the hub cache". Two lists, because they answer different questions: closest
    by name (did it get renamed, re-quantized, moved orgs) and smallest indexed (what can actually
    fill a slot whose whole job is to be cheap). Sizes ride along, because "which model instead" is
    a budget question on a box with one aperture.
    """
    rows = rows if rows is not None else _rows()
    names = [str(r["id"]) for r in rows]
    by_name = difflib.get_close_matches(str(missing), names, n=limit, cutoff=0.55)
    # "Smallest" is only a useful answer if the thing can hold the slot. The two smallest rows on
    # this box are an embedder and a reranker: 0.31 and 0.60 GiB, and neither serves a chat.
    generative = [r for r in rows if not r.get("pooling_type")]
    by_size = sorted(generative, key=lambda r: r.get("bytes") or (1 << 62))[:limit]
    return {"by_name": [{"id": n} for n in by_name],
            "smallest": [{"id": str(r["id"]), "bytes": r.get("bytes")} for r in by_size]}


def _availability(missing: str) -> str:
    """The tail of a refusal: candidates, with sizes. Never a guess presented as a fix."""
    rows = _rows()
    if not rows:
        return " (the model index is empty — nothing to name)"
    sug = suggest_for(missing, rows)
    parts = []
    if sug["by_name"]:
        parts.append("closest by name: " + ", ".join(s["id"] for s in sug["by_name"]))
    if sug["smallest"]:
        parts.append("smallest that can serve a chat slot: " + ", ".join(
            f"{s['id']} ({(s['bytes'] or 0) / (1024 ** 3):.2f} GiB)" for s in sug["smallest"]))
    return (" — " + "; ".join(parts)) if parts else ""


def resolve_models(personality: dict) -> list[str]:
    """The library model ids a personality covers. See resolve_targets for what an entry may be."""
    return [t["model"] for t in resolve_targets(personality)]


#: llama.cpp's `parallel = -1` means "decide for me", and what it decides is measurable: 4 slots
#: over a unified KV pool on this box (PORTAL_PLAN §6.12.2). Checkpoint copies are per slot, so an
#: unset `parallel` still costs 4 copies, and pricing it as 1 would understate the request.
AUTO_PARALLEL_SLOTS = 4


def _layered_settings(personality: dict, targets: list[dict] | None = None,
                      configs: dict[str, dict] | None = None) -> dict[str, dict[str, Any]]:
    """The model sections this personality produces, keyed by library model id.

    One implementation on purpose. `render()` writes these dicts into the file and `budget_for()`
    prices them; a budget computed from a different layering than the file that gets written is a
    verdict about a file that will never exist. The order is the router's own: `[*]` is the floor
    (added by `_effective()`, not here), the model config sits on top of it, and a personality-level
    override sits on top of the config.
    """
    if targets is None:
        targets = resolve_targets(personality)
    if configs is None:
        configs = {c["name"]: c for c in model_config.rows()}
    overrides = personality.get("overrides") or {}
    out: dict[str, dict[str, Any]] = {}
    for target in targets:
        conf = configs.get(target["config"]) if target["config"] else None
        per: dict[str, Any] = dict((conf or {}).get("settings") or {})
        per.update(dict(overrides.get(target["model"]) or {}))
        out[target["model"]] = per
    return out


def _effective(personality: dict, model_id: str, key: str, fallback: Any,
               layered: dict[str, Any] | None = None) -> Any:
    """The value a key will actually have for one model: model section, then [*], then llama.cpp's
    own default. Not the portal's GLOBAL_DEFAULTS -- these keys are deliberately not in it, so an
    unset key means the server's default, not a portal-chosen one.

    `layered` is that model section already resolved, by `_layered_settings()`: the config layer with
    the personality's override on top of it. Passing it is what keeps a priced budget and a written
    file answering the same question; without it only the override layer is seen, which is what a
    pre-config personality looks like.
    """
    for source in (layered, (personality.get("overrides") or {}).get(model_id) or {}):
        if source and key in source:
            return source[key]
    glob = personality.get("global") or {}
    if key in glob:
        return glob[key]
    return fallback


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
    targets = resolve_targets(personality)
    # One layering, shared with render(): the settings each model will actually receive.
    layered = _layered_settings(personality, targets)

    peaks: list[tuple[int, str]] = []
    for target in targets:
        mid = target["model"]
        row = rows.get(mid)
        if row is None:
            out.unpriced.append(mid)
            continue
        # The context this model is going to get. `peak_bytes(row)` prices the index's own estimate,
        # which is a context the file may never ask for: a model config that sets ctx-size changes
        # the footprint, so a worst case that ignores it is the price of a different personality
        # than the one being applied (PORTAL_PLAN §4.10.4).
        per = layered.get(mid) or {}
        ctx = modelmeta._as_int(per.get("ctx-size"))
        peak = modelindex.peak_bytes(row, ctx)
        if peak is None and ctx:
            peak = _conservative_peak(row, ctx)
        if peak is None:
            # Unpriceable is not "free": it means we cannot prove this fits.
            out.unpriced.append(mid)
            continue
        peaks.append((peak, mid))
        out.priced.append({"id": mid, "peak_bytes": peak,
                           "ctx_priced": ctx if ctx else row.get("est_ctx"),
                           "ctx_from": "section" if ctx else "index estimate"})

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

    # The aperture is not the whole accounting. Every loaded model's server also holds host RAM --
    # the prompt cache (8192 MiB by default) and its context checkpoints -- and on this box that is
    # the same physical memory the 108.00 GiB GTT aperture is carved out of (122.83 GiB installed).
    # A personality that fits the aperture exactly leaves nothing for the caches, and the failure
    # that produces does not look like a GPU error at all: it looks like the kernel killing
    # llama-server, which is the shape the OOM events on this box actually had.
    host_total: int | None = 0
    for _, mid in take:
        per = layered.get(mid) or {}
        slots = _effective(personality, mid, "parallel", AUTO_PARALLEL_SLOTS, per)
        h = modelindex.host_cache_bytes(
            rows[mid],
            checkpoints=_effective(personality, mid, "ctx-checkpoints",
                                   modelindex.DEFAULT_CTX_CHECKPOINTS, per),
            parallel=AUTO_PARALLEL_SLOTS if not slots or slots < 1 else slots,
            cache_ram_mib=_effective(personality, mid, "cache-ram",
                                     modelindex.DEFAULT_CACHE_RAM_MIB, per))
        if h["total_bytes"] is None:
            # An unbounded prompt cache (cache-ram = -1) or an unpriceable model: there is no
            # number to check, so the RAM question stays unanswered rather than answered "yes".
            host_total = None
            out.host_notes.extend(h["notes"])
        elif host_total is not None:
            host_total += h["total_bytes"]
    out.host_cache_bytes = host_total
    out.ram_total_bytes = modelindex.ram_total_bytes()
    if host_total is not None and out.ram_total_bytes:
        need = out.worst_case_bytes + host_total
        out.ram_headroom_bytes = out.ram_total_bytes - need
        out.fits_ram = need <= out.ram_total_bytes

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
    if isinstance(value, bool):
        return "on" if value else "off"
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


def _ini_text(sections: dict[str, dict[str, Any]],
              personality_name: str | None = None) -> str:
    """Render an ini body. Comments carry the evidence, because a bare number in a file
    the router reads is exactly the thing a future debugger cannot question.

    Ordering is explicit and stable: [*] first (the router applies it as the base), then
    model sections sorted by name, so a diff between two generations is a diff of intent
    and not of dict ordering.
    """
    lines = [
        "# Generated by ai-portal app/core/personality.py",
        # The name is not decoration. A personality that names models without setting anything for
        # them produces the same body as one that names a different set — sections carry settings,
        # not an allow-list — and "which personality is live" is answered by comparing this file
        # against what render() would write. Without a name in the file, `safe` and `default` are
        # both live at once, which is not a thing the router can be.
        *( [f"# personality: {personality_name}"] if personality_name else []),
        "# Section names are directory names under ~/ai/models/llama, or model ids for models",
        "# found in the Hugging Face cache (measured: the router lists such a section as",
        "# `[ preset] <repo:TAG>`, and its settings attach to that cached file).",
        "# Keys are llama-server args (no leading dash); see llama.cpp tools/server/README.md.",
        "# Router-only keys understood here: " + ", ".join(ROUTER_ONLY_KEYS),
        "# A model section may set `alias = <name>`: the router then routes by that name too, while",
        "# the section stays keyed by the directory name. Aliases share one namespace across models.",
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
    targets = resolve_targets(personality)
    models = [t["model"] for t in targets]
    configs = {c["name"]: c for c in model_config.rows()}

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
    # Keys registered as model-scoped are refused in [*], because [*] applies to every model the
    # router *discovers*, not to the models this personality listed. Measured on this box: `[*]
    # load-on-startup = on` began loading a local 0.8B model *and* `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL`
    # found in the shared HF cache, and `models-max = 1` in the same file did not stop either one
    # (§4.10.2). A personality must not be able to reach models it never named.
    scoped = sorted(k for k in glob if SETTINGS.get(k, {}).get("section") == "model")
    if scoped:
        raise PersonalityError(
            f"personality {name!r} sets {scoped} in [*]; that section reaches every model the router "
            f"discovers, including cached ones this personality never lists -- set it in that "
            f"model's own section instead")

    sections["*"].update(glob)

    # The layering is the router's own, and it lives in one function: [*] is the floor, the model
    # config sits on top of it, and a personality-level override sits on top of the config. The page
    # creates the middle layer only; the top layer exists for payloads assembled by hand and must
    # still resolve. budget_for() prices this same result, so a card's fit verdict is about the file
    # underneath it rather than about the library's defaults.
    layered = _layered_settings(personality, targets, configs)
    for target in targets:
        mid = target["model"]
        conf = configs.get(target["config"]) if target["config"] else None
        per: dict[str, Any] = dict(layered[mid])
        hostile = sorted(k for k in per if k.lower() in ROUTER_CONTROLLED_KEYS)
        if hostile:
            # The router removes or overwrites these on load; emitting them would look
            # like a setting and do nothing.
            raise PersonalityError(
                f"personality {name!r} sets router-controlled key(s) {hostile} for "
                f"{mid}; the router overwrites them"
            )
        if conf:
            # Injected after that refusal, on purpose: `alias` is a router-controlled key in every
            # direction a person can type it, and it is refused in [*] as a model-scoped key. The
            # only way it reaches the file is as the config's own name, because that name has been
            # checked against every model id and every other config -- a collision the router
            # answers by not starting at all (measured, §4.10.4).
            per["alias"] = conf["name"]
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

    text = _ini_text(sections, name)
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


#: A personality name and a model config name have to survive the same three things -- an ini
#: value, the router's comma-split of `--alias`, and being rendered into the page -- so they share
#: one alphabet rather than having two rules that drift.
NAME_RE = model_config.NAME_RE
RESERVED_NAMES = ("safe", "default")


def name_problem(name: str) -> str | None:
    """Why this cannot be a personality name, or None. `safe` and `default` are refused because
    they are computed from the library: storing an editable copy of them would mean the page shows
    one thing and the router runs another the day the library changes."""
    if not isinstance(name, str) or not name.strip():
        return "a personality needs a name"
    if name.lower() in RESERVED_NAMES:
        return (f"{name!r} is a built-in personality: it is derived from the library and is not "
                f"edited in place -- save a new name instead")
    if not NAME_RE.fullmatch(name):
        return ("a personality name may use letters, digits, dot, dash and underscore, starting "
                "with a letter or digit")
    return None


def stored() -> list[dict]:
    """Custom personalities, as stored. JSON columns are decoded here so no caller has to
    remember which of them were JSON."""
    out: list[dict] = []
    for row in db.query("SELECT * FROM personalities ORDER BY name"):
        p = {"name": row["name"], "models_max": row["models_max"], "note": row["note"] or "",
             "models": None, "global": {}, "built_in": False,
             "created_at": row["created_at"], "updated_at": row["updated_at"]}
        try:
            p["models"] = json.loads(row["models"]) if row["models"] is not None else None
        except (TypeError, ValueError):
            p["models"] = []
            p["corrupt"] = "models"
        try:
            p["global"] = json.loads(row["global"] or "{}")
        except (TypeError, ValueError):
            p["global"] = {}
            p["corrupt"] = "global"
        out.append(p)
    return out


def all_personalities() -> list[dict]:
    """Everything the page and the API may offer: the computed pair first, then custom ones."""
    return builtins() + stored()


def find(name: str) -> dict | None:
    for p in all_personalities():
        if p.get("name") == name:
            return p
    return None


def _clean_settings(where: str, raw: dict | None, errors: list[str]) -> dict:
    """One field-by-field pass over proposed settings: defined, writable, right type, allowed value.

    Shared by the personality editor and the model-config editor because both are the same
    question -- "may this key take this value in this section" -- and two answers to it is how one
    of them gets relaxed.
    """
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


def save_personality(name: str, models_max: Any = 1, note: str = "",
                     models: list[str] | None = None, global_edits: dict | None = None,
                     budget_bytes: int | None = None) -> dict:
    """Create or replace a custom personality, refusing before anything is written.

    The stored form is deliberately small: identity, models_max, which models/configs it assigns,
    and `[*]`. Per-model settings are not stored here any more -- they live in the model config the
    personality assigns (§4.10.4) -- so there is one place to edit a value and no layer to wonder
    about. `overrides` is still honoured when a personality dict is assembled by hand, because
    render() and apply() are older than this and their tests describe real behaviour.

    It saves through render(), not alongside it: a personality that cannot be generated is not a
    personality, and accepting it here would only move the failure to the moment someone presses
    Apply.
    """
    problem = name_problem(name)
    if problem:
        raise PersonalityError(problem)

    candidate = {"name": name, "models_max": models_max, "note": note, "models": models,
                 "global": {}}
    _models_max(candidate)                      # refuses a non-positive/non-numeric models_max

    errors: list[str] = []
    glob = _clean_settings("[*]", global_edits, errors)
    if errors:
        raise PersonalityError("the proposed settings were refused:\n  " + "\n  ".join(errors))

    candidate["global"] = glob
    targets = resolve_targets(candidate)         # unknown names, and two configs on one model

    render(candidate, budget_bytes)              # the same refusal Apply would give

    now = time.time()
    existing = db.query("SELECT created_at FROM personalities WHERE name = ?", (name,))
    created = existing[0]["created_at"] if existing else now
    db.execute(
        """INSERT INTO personalities (name, models_max, note, models, global, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             models_max=excluded.models_max, note=excluded.note, models=excluded.models,
             global=excluded.global, updated_at=excluded.updated_at""",
        (name, candidate["models_max"], note,
         json.dumps(models if models is not None else None, sort_keys=True),
         json.dumps(glob, sort_keys=True), created, now))
    db.event("portal", "personality",
             f"saved {name}: models_max={candidate['models_max']}, "
             f"{'whole library' if models is None else f'{len(targets)} model(s)'}")
    return find(name) or candidate


def delete_personality(name: str) -> bool:
    """Drop a custom personality. The built-in pair cannot be deleted: they are what the page falls
    back to, and a box with no personalities is a box with no way back."""
    if name.lower() in RESERVED_NAMES:
        raise PersonalityError(f"{name!r} is built-in and cannot be deleted")
    row = db.query("SELECT name FROM personalities WHERE name = ?", (name,))
    if not row:
        return False
    db.execute("DELETE FROM personalities WHERE name = ?", (name,))
    db.event("portal", "personality", f"deleted {name}")
    return True


def normalize_config(name: str, base_model: str, raw_settings: dict | None,
                     note: str = "") -> dict:
    """Validate a model config: a usable name, a real base model, and settings this section may carry.

    The name check is not housekeeping. `render()` writes the name as `alias`, the router keeps
    aliases in one namespace across every model it discovers, and a collision is a router that exits
    1 before loading anything -- so the refusal has to happen here, where there is a form to answer
    to, rather than at the next restart.
    """
    errors: list[str] = []
    problem = model_config.name_problem(name, base_model, allow_self=True)
    if problem:
        errors.append(f"name: {problem}")
    settings = _clean_settings(f"[{name or '?'}]", raw_settings, errors)
    if errors:
        raise PersonalityError("the proposed model config was refused:\n  "
                               + "\n  ".join(errors))
    return {"name": name, "base_model": base_model, "settings": settings, "note": note}


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


def _alias_problems(section: str, where: str, value: str, known_models: set[str],
                    seen_alias: dict[str, str]) -> list[str]:
    """Why this `alias` line must not be written, or [] if the router can live with it."""
    if section == "*":
        return [f"{where} alias: [*] applies to every model the router discovers, so one alias "
                f"there names all of them -- put the alias in that model's own section"]
    if not NAME_RE.fullmatch(value or ""):
        return [f"{where} alias {value!r}: a name the router can be given must use letters, "
                f"digits, dot, dash or underscore (it splits aliases on commas)"]
    if value in known_models:
        return [f"{where} alias {value!r} is already a model in the library: the router keeps "
                f"names and aliases in one namespace and refuses to start on a collision"]
    if value in seen_alias:
        return [f"{where} alias {value!r} is already the alias of {seen_alias[value]!r}: two "
                f"models answering to one name is another collision the router will not accept"]
    return []


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
    seen_alias: dict[str, str] = {}
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
            if key == "alias":
                # The one router-controlled key the portal does write, and only as a config's name
                # in that model's own section. It is checked here rather than trusted because this
                # is the last point where a file can still be refused: the router's own answer to an
                # alias collision is to exit 1 without loading anything (§4.10.4), which is not a
                # failure that arrives as a useful message later.
                problems.extend(_alias_problems(section, where, value, known_models, seen_alias))
                if value:
                    seen_alias.setdefault(value, section)
                continue
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
        return _clean_settings(where, raw, errors)

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
    # A separate refusal for the other ceiling, because a personality can clear the aperture and
    # still be unwritable on this box. The message names both numbers so the operator can see which
    # ceiling they are actually up against.
    if budget["fits_ram"] is FITS_NO:
        raise PersonalityError(
            f"refusing to write personality {rendered['name']!r}: its heaviest "
            f"{rendered['models_max']}-model combination costs "
            f"{_gib(budget['worst_case_bytes'])} plus {_gib(budget['host_cache_bytes'])} of "
            f"host-side prompt cache and context checkpoints = "
            f"{_gib(budget['worst_case_bytes'] + budget['host_cache_bytes'])}, over the "
            f"{_gib(budget['ram_total_bytes'])} of RAM this machine has"
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
    if b.fits_ram is FITS_NO:
        worst = b.worst_case_bytes or 0
        caches = b.host_cache_bytes or 0
        findings.append({
            "severity": "bad", "kind": "personality",
            "message": (
                f"worst case {_gib(worst)} plus {_gib(caches)} of "
                f"host-side caches = {_gib(worst + caches)} exceeds the "
                f"{_gib(b.ram_total_bytes)} of RAM this machine has: the aperture is not the only "
                f"ceiling, and llama.cpp's prompt cache and context checkpoints live in the same "
                f"physical memory the aperture is carved from"
            ),
        })
    elif b.fits_ram is FITS_UNKNOWN:
        findings.append({
            "severity": "warn", "kind": "personality",
            "message": "host-side cache cost cannot be bounded, so the RAM ceiling cannot be "
                       f"checked: {'; '.join(b.host_notes) or 'no RAM total readable'}",
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
        "editable": name.lower() not in RESERVED_NAMES,
        "built_in": name.lower() in RESERVED_NAMES,
        "configs": [],
        "findings": [],
        "context": [],
        "generated_at": time.time(),
    }
    # One try for the whole evaluation: models_max parsing, model resolution and budget
    # math can all raise, and describe() renders a page. A half-built dict with a valid
    # models_max and no findings would read as "this personality is fine".
    try:
        out["models_max"] = _models_max(personality)
        # Configs are named here as well as models: the page has to distinguish "this file, under
        # the name the file gave it" from "this file, through a config that renames it and carries
        # settings", because those two look identical in a list of model ids.
        targets = resolve_targets(personality)
        out["models"] = [t["model"] for t in targets]
        out["configs"] = [{"name": t["config"], "model": t["model"]} for t in targets
                          if t["config"]]
        out["findings"] = validate(personality, budget_bytes)
        # Context per model, from the same predictor the context-window tests pin. Rendered
        # as "what a client gets here", so the number an operator is buying is visible next
        # to the number the box pays for it (sec 6.12.22).
        out["context"] = context_for(personality, targets=targets, budget_bytes=budget_bytes)
    except PersonalityError as exc:
        out["models"] = []
        out["error"] = str(exc)
        out["findings"] = [{"severity": "bad", "kind": "personality", "message": str(exc)}]
    return out


# --------------------------------------------------- what a personality costs a client
CTX_KEYS = ("parallel", "ctx-size", "kv-unified", "kv-unified-per-slot")


def context_for(personality: dict, targets: list[dict] | None = None,
                rows: list[dict] | None = None,
                budget_bytes: int | None = None) -> list[dict]:
    """Tokens each model hands a client, who decided it, and what that cache costs.

    A personality names models and settings. Until now the page showed the *worst-case
    resident cost* and nothing about the context a client gets, so an operator choosing
    between two personalities could not see that one hands 16384 tokens per request and
    the other 2048 -- the number that decides whether a coding session fits at all
    (docs/PORTAL_PLAN.md sec 6.12.22 measures how a window is set).

    Uses the portal's existing measuring code, not a second formula:
    `modelindex.context_window()` for the window (the same predictor `tests/
    test_context_window.py` pins to measured server output) and `_conservative_peak()`
    for the cost, which is the sanctioned fallback when the index cannot reprice a
    context. Both directions of that fallback err toward over-estimating, which is the
    only safe direction for a capacity number shown next to an Apply button.

    A model that cannot be priced says so. `window: None` with a reason beats an
    optimistic token count.
    """
    targets = targets if targets is not None else resolve_targets(personality)
    if rows is None:
        rows = modelindex.rows()
    by_key = {}
    for r in rows:
        by_key.setdefault(modelindex.router_key(r.get("id") or ""), r)
    layered_all = _layered_settings(personality, targets)
    pool = budget_bytes if budget_bytes is not None else modelindex._pool_bytes()

    out: list[dict] = []
    for target in targets:
        model = target["model"]
        row = by_key.get(modelindex.router_key(model))
        per = layered_all.get(model) or {}
        settings = {k: _effective(personality, model, k, None, layered=per) for k in CTX_KEYS}
        settings = {k: v for k, v in settings.items() if v is not None and v != ""}
        entry: dict = {"model": model, "window": None, "by": "", "cost": None,
                       "priced": False, "notes": [], "settings": settings}
        if not row:
            entry["by"] = "not in the model index, so nothing is known about this model"
            entry["notes"].append("rescan the index (Models page) so the header can be read")
            out.append(entry)
            continue
        pred = modelindex.context_window(row, settings, budget_bytes=pool,
                                       price=_conservative_peak)
        window = modelmeta._as_int(pred.get("window")) or 0
        entry["window"] = window or None
        entry["by"] = pred.get("by") or ""
        entry["notes"] = list(pred.get("notes") or [])
        entry["kv_unified"] = pred.get("kv_unified")
        entry["slots"] = pred.get("slots")
        entry["pool"] = modelmeta._as_int(pred.get("pool"))
        if window > 0:
            cost = _conservative_peak(row, window)
            if cost is None:
                cost = modelindex.peak_bytes(row, window)
            if cost:
                entry["cost"] = cost
                entry["priced"] = True
        if not entry["priced"]:
            entry["notes"].append("cache cost is not predictable for this file "
                                  "(no weights or cache figure in the index)")
        out.append(entry)
    return out
