"""llama.cpp router log -> a picture of what the router is doing *right now*.

Why this exists
---------------
The router's log is information-dense and, taken literally, useless at a glance:
during one generation it prints a line every ~3 seconds that differs from the
previous one only in numbers::

    [55601] 905.38.610.130 I slot print_timing: id  1 | task 282178 | n_gen =  512, tg =   5.97 t/s, tg_3s =   6.02 t/s
    [55601] 905.41.614.660 I slot print_timing: id  1 | task 282178 | n_gen =  530, tg =   5.97 t/s, tg_3s =   5.99 t/s

Answering "is it working on my request, or wedged?" means diffing those by eye,
while the lines that matter — a model being swapped in, a slot that hit the
context limit, a cancelled request — scroll away behind them.

This module reads the journal tail and returns what a person managing the router
actually wants: per-slot phase and progress, a time-binned throughput history,
which model is loaded / loading / being evicted, one-line request stats, and a
short list of things worth noticing. It is a pure function of the log — no I/O,
no caching, no clock dependency in tests — so the same input always yields the
same picture. That is also what lets the dashboard chart show history the
instant the page opens instead of after it has been sitting there for five
minutes.

Line kinds (verified against b10913 in router mode, and against
``tools/server/server-models.cpp`` for the rarer router messages)
-----------------------------------------------------------------
per slot, per task::

    slot get_availabl: id  1 | task -1 | selected slot by LCP similarity, f_sim_best = 0.996 (> 0.100 thold), f_keep = 1.000
    slot launch_slot_: id  1 | task 283045 | processing task, is_child = 0
    slot print_timing: id  1 | task 283045 | prompt processing, n_tokens = 779, progress = 1.00, t = 3.53 s / 220.72 tokens per second
    slot print_timing: id  1 | task 283045 | n_gen = 100, tg = 5.89 t/s, tg_3s = 5.95 t/s
    slot print_timing: id  1 | task 283045 | prompt eval time = 9204.97 ms / 783 tokens (  11.76 ms per token,  85.06 tokens per second)
    slot print_timing: id  1 | task 283045 |        eval time = 96291.85 ms / 572 tokens ( 168.64 ms per token,   5.93 tokens per second)
    slot print_timing: id  1 | task 283045 |       total time = 105496.82 ms / 1355 tokens
    slot      release: id  1 | task 283045 | stop processing: n_tokens = 199524, truncated = 0

router::

    srv  proxy_reques: proxying request to model Qwen3.8-Flash-Next-GGUF on port 55601
    model name=X is not loaded, loading...
    waiting until model name=X is fully loaded...
    evicting idle LRU name=X for a queued request
    slot available, loading queued model name=X
    (startup) loading model X

never-scroll-past warnings::

    W find_slot: non-consecutive token position 513 after 511 for sequence ...
    W srv alloc: ... - making room for prompt cache entry, removing oldest entry (size = 32.0 MiB)
    W srv stop: cancel task, id_task = 1234
    E srv operator(): http client error: Connection handling canceled

Timestamps
----------
``journalctl -o cat`` gives the message only, and llama.cpp prefixes it with
``[port] min.sec.ms.us L `` — minutes/seconds/ms/microseconds since the process
started (``common/log.cpp``). That is a precise monotonic clock for durations.
Wall-clock time comes from the journal when the caller passes
``(epoch, message)`` pairs; without them, ages are relative to the newest line.
"""
from __future__ import annotations

import re

# "[55601] 906.37.818.813 I " — router-level lines omit the [port] tag.
LINE_RE = re.compile(r"^(?:\[\d+\]\s+)?(\d+)\.(\d{2})\.(\d{3})\.(\d{3})\s+([EWID])\s+(.*)$")
SLOT_RE = re.compile(r"\bid\s+(\d+)\s*\|\s*task\s+(-?\d+)")

PROMPT_PROGRESS_RE = re.compile(
    r"prompt processing, n_tokens\s*=\s*(\d+),\s*progress\s*=\s*([\d.]+),\s*t\s*=\s*([\d.]+)\s*s\s*/\s*([\d.]+)\s*tokens per second")
GEN_RE = re.compile(r"\bn_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s(?:,\s*tg_3s\s*=\s*([\d.]+)\s*t/s)?")
PROMPT_EVAL_RE = re.compile(
    r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens\s*\(.*?([\d.]+)\s*tokens per second")
# the prompt-summary line also ends in "eval time", so exclude that spelling
GEN_EVAL_RE = re.compile(
    r"(?<!prompt )eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens\s*\(.*?([\d.]+)\s*tokens per second")
TOTAL_RE = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
RELEASE_RE = re.compile(r"stop processing:\s*n_tokens\s*=\s*(\d+),\s*truncated\s*=\s*(\d+)")
CACHE_KEEP_RE = re.compile(r"f_keep\s*=\s*([\d.]+)")

PROXY_RE = re.compile(r"proxying request to model\s+(\S+)")
NOT_LOADED_RE = re.compile(r"model name=(\S+) is not loaded, loading")
WAITING_RE = re.compile(r"waiting until model name=(\S+) is fully loaded")
EVICT_RE = re.compile(r"evicting idle LRU name=(\S+)")
QUEUE_LOAD_RE = re.compile(r"slot available, loading queued model name=(\S+)")
STARTUP_LOAD_RE = re.compile(r"\(startup\) loading model (\S+)")

# Lines that deserve the attention feed even though they are level I. Each entry
# is (substring, human sentence template with {slot} available).
NOTABLE = (
    ("non-consecutive token position", "cache miss: prompt position mismatch — slot re-decodes context"),
    ("making room for prompt cache", "prompt cache pressure — evicting the oldest cached context"),
    ("cancel task", "a request was cancelled before it finished"),
    ("did not go through", "a queued model load failed"),
    ("force-killing", "a model load had to be force-killed"),
    ("is still loading", "a model is still loading"),
    ("no available slot", "no free slot for a new request"),
    ("out of memory", "out of memory"),
    ("download failed", "a model download failed"),
)
LEVELS = {"E": "error", "W": "warn", "I": "info", "D": "debug"}
# a generation count above this is a stale read from a superseded task
_MAX_GEN = 10_000_000
# seconds of silence from a slot that still believes it is busy
_STALL_AGE_S = 15.0
# keep claiming "loading" for at most this long after the last evidence of it
_LOADING_MAX_AGE = 900.0


def _proc_seconds(mins: str, secs: str, ms: str, us: str) -> float:
    """llama.cpp stamps each line as minutes.seconds.ms.us since process start."""
    return int(mins) * 60 + int(secs) + int(ms) / 1000 + int(us) / 1_000_000


def _collapse(items: list[dict], wall: float) -> list[dict]:
    """Fold repeated notices into one line with a count.

    Cache pressure and position-mismatch warnings arrive dozens of times per
    minute; ten copies of one sentence is the wall of look-alike lines this
    whole pane exists to avoid. The newest occurrence keeps its place, the rest
    become a count.
    """
    out: list[dict] = []
    index: dict[tuple, dict] = {}
    for item in items:
        key = (item["level"], item["text"])
        if key in index:
            entry = index[key]
            entry["count"] += 1
            if item["age_s"] < entry["age_s"]:
                entry["age_s"] = item["age_s"]
                entry["age_text"] = item["age_text"]
            out.append(out.pop(out.index(entry)))   # last occurrence wins the order
            continue
        entry = dict(item)
        entry["count"] = 1
        index[key] = entry
        out.append(entry)
    return out


def _dur(seconds: float | None) -> str | None:
    """Human duration, computed once so the template and the browser cannot
    drift apart on how "4m 12s" looks."""
    if seconds is None:
        return None
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s / 3600:.1f}h"


def _clip(text: str, n: int = 180) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


class _Slot:
    """Mutable per-slot state assembled while scanning the log."""

    __slots__ = ("id", "task", "state", "prompt_tokens", "prompt_done", "prompt_pct",
                 "prompt_tps", "prompt_ms", "gen_tokens", "gen_tps", "gen_tps_recent",
                 "gen_ms", "total_ms", "total_tokens", "truncated", "t_start", "t_last")

    def __init__(self, slot_id: int):
        self.id = slot_id
        self.task: int | None = None
        self.state = "idle"          # idle | queued | prompt | generate
        self.prompt_tokens = 0
        self.prompt_done = 0
        self.prompt_pct: float | None = None
        self.prompt_tps: float | None = None
        self.prompt_ms: float | None = None
        self.gen_tokens = 0
        self.gen_tps: float | None = None
        self.gen_tps_recent: float | None = None
        self.gen_ms: float | None = None
        self.total_ms: float | None = None
        self.total_tokens: int | None = None
        self.truncated = False
        self.t_start: float | None = None
        self.t_last: float | None = None

    def begin(self, task: int, t: float) -> None:
        """A new task arrived on this slot: reset everything task-scoped."""
        if task != self.task:
            self.task = task
            self.t_start = t
            self.prompt_tokens = self.prompt_done = 0
            self.prompt_pct = self.prompt_tps = self.prompt_ms = None
            self.gen_tps = self.gen_tps_recent = None
            self.gen_ms = self.total_ms = self.total_tokens = None
            self.truncated = False
        self.gen_tokens = 0

    def as_dict(self, now: float) -> dict:
        elapsed = (self.t_last - self.t_start) if (self.t_start and self.t_last) else None
        # A running slot prints a timing line every ~3s. When that stops while the
        # slot is still marked busy, it is waiting for something outside itself
        # (a streaming client, a tool call, the queue) - worth saying out loud
        # rather than showing a bar frozen mid-flight.
        age = max(0.0, now - self.t_last) if self.t_last else None
        stalled = bool(age is not None and age > _STALL_AGE_S
                        and self.state in ("prompt", "generate"))
        return {
            "stalled": stalled,
            "id": self.id,
            "task": self.task,
            "state": self.state,
            "prompt_tokens": self.prompt_tokens,
            "prompt_done": self.prompt_done,
            "prompt_pct": self.prompt_pct,
            "prompt_tps": self.prompt_tps,
            "gen_tokens": self.gen_tokens,
            "gen_tps": self.gen_tps,
            "gen_tps_recent": self.gen_tps_recent,
            "total_ms": self.total_ms,
            "total_tokens": self.total_tokens,
            "truncated": self.truncated,
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "elapsed_text": _dur(elapsed),
            "age_s": round(max(0.0, now - self.t_last), 1) if (self.t_last and now) else None,
            "age_text": _dur(max(0.0, now - self.t_last)) if self.t_last else None,
        }


def parse(lines, *, window_s: int = 420, bin_s: int = 5,
          now_wall: float | None = None, max_attention: int = 30) -> dict:
    """Turn journal lines into the live picture described in the docstring.

    ``lines`` may be message strings or ``(epoch, message)`` pairs. With pairs,
    ages and the chart axis are wall-clock; without, they are process-relative
    (still consistent, just not anchored to the wall clock).
    """
    prepared: list[tuple[float, str, str]] = []
    anchor: list[float | None] = [None]     # wall epoch of stamp 0
    prev_proc = 0.0
    has_epoch = False

    for entry in lines or []:
        epoch, text = entry if isinstance(entry, (tuple, list)) else (None, entry)
        text = (text or "").rstrip("\n")
        if not text.strip():
            continue
        m = LINE_RE.match(text)
        if m:
            proc = _proc_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
            level, body = LEVELS.get(m.group(5), "info"), m.group(6)
        else:
            # continuation line, or an unstamped build: keep ordering, inherit time
            proc, level, body = prev_proc, "info", text
        prev_proc = proc
        if epoch is None:
            t = proc
        else:
            has_epoch = True
            if anchor[0] is None:
                anchor[0] = float(epoch) - proc
            t = anchor[0] + proc
        prepared.append((t, level, re.sub(r"\s+", " ", body).strip()))

    wall = now_wall if now_wall is not None else (prepared[-1][0] if prepared else 0.0)

    slots: dict[int, _Slot] = {}
    attention: list[dict] = []
    requests: list[dict] = []
    completions: list[dict] = []
    bins: dict[int, dict[str, float]] = {}
    # prompt rates ride the same 5 s grid in their own dict: prefill runs at
    # 5-30x generation speed, so the chart keeps it on its own scale
    pbins: dict[int, dict[str, float]] = {}
    keeps: list[float] = []
    prompt_rates: list[float] = []
    gen_rates: list[float] = []
    loaded: str | None = None
    loading: str | None = None
    loading_t: float | None = None
    waiting_for_slot = False
    evictions = 0

    def note(level: str, text: str, t: float) -> None:
        age = max(0.0, wall - t)
        attention.append({"level": level, "text": _clip(text),
                          "age_s": round(age, 1), "age_text": _dur(age)})

    for t, level, body in prepared:
        # ---------------------------------------------------------- router
        if "proxying request to model" in body:
            m = PROXY_RE.search(body)
            if m:
                name = m.group(1)
                requests.append({"model": name, "t": t})
                loaded = name
            continue
        if m := NOT_LOADED_RE.search(body):
            name = m.group(1)
            if name != loading or loading_t is None:
                loading_t = t          # measure the switch from its first evidence
            loading, waiting_for_slot = name, False
            continue
        if m := WAITING_RE.search(body):
            # the load is queued behind work still running on the other slots
            waiting_for_slot = True
            loading = m.group(1)
            continue
        if m := QUEUE_LOAD_RE.search(body):
            loading, waiting_for_slot = m.group(1), False
            continue
        if m := EVICT_RE.search(body):
            evictions += 1
            note("notice", f"model swap: evicting {m.group(1)} to make room for a queued request", t)
            continue

        # ------------------------------------------------------------ slots
        if sm := SLOT_RE.search(body):
            # Slot activity is the proof that a model is live: llama.cpp only prints
            # these from the instance that is actually running. A `proxying request`
            # line does NOT prove it - that line is what triggers the load, so using
            # it would hide the switch exactly while the user is waiting for it.
            if loading and not waiting_for_slot and (
                    "launch_slot_" in body or "print_timing" in body or "release:" in body):
                loading = None
            slot = slots.setdefault(int(sm.group(1)), _Slot(int(sm.group(1))))
            task = int(sm.group(2))
            slot.t_last = t
            if "get_availabl" in body:
                if ck := CACHE_KEEP_RE.search(body):
                    keeps.append(max(0.0, min(1.0, float(ck.group(1)))))
                if slot.state == "idle":
                    slot.state = "queued"
                continue
            if "launch_slot_" in body:
                slot.begin(task, t)
                slot.state = "prompt"
                continue
            if "print_timing" in body:
                if pm := PROMPT_PROGRESS_RE.search(body):
                    if slot.task != task:
                        slot.begin(task, t)
                    slot.state = "prompt"
                    slot.prompt_done = int(pm.group(1))
                    slot.prompt_tokens = max(slot.prompt_tokens, slot.prompt_done)
                    slot.prompt_pct = max(0.0, min(1.0, float(pm.group(2))))
                    slot.prompt_tps = float(pm.group(4))
                    prompt_rates.append(slot.prompt_tps)
                    pbins.setdefault(int(t / bin_s) * bin_s, {})[str(slot.id)] = slot.prompt_tps
                    continue
                if gm := GEN_RE.search(body):
                    n = int(gm.group(1))
                    if n <= _MAX_GEN:
                        if slot.task != task:
                            slot.begin(task, t)
                        slot.state = "generate"
                        slot.gen_tokens = n
                        slot.gen_tps = float(gm.group(2))
                        slot.gen_tps_recent = float(gm.group(3) or gm.group(2))
                        gen_rates.append(slot.gen_tps)
                        bins.setdefault(int(t / bin_s) * bin_s, {})[str(slot.id)] = slot.gen_tps
                    continue
                if pem := PROMPT_EVAL_RE.search(body):
                    slot.prompt_ms = float(pem.group(1))
                    slot.prompt_tokens = int(pem.group(2))
                    slot.prompt_done = slot.prompt_tokens
                    slot.prompt_pct = 1.0
                    slot.prompt_tps = float(pem.group(3))
                    prompt_rates.append(slot.prompt_tps)
                    pbins.setdefault(int(t / bin_s) * bin_s, {})[str(slot.id)] = slot.prompt_tps
                    continue
                if gem := GEN_EVAL_RE.search(body):
                    slot.gen_ms = float(gem.group(1))
                    slot.gen_tokens = int(gem.group(2))
                    slot.gen_tps = float(gem.group(3))
                    slot.state = "generate"
                    gen_rates.append(slot.gen_tps)
                    continue
                if tm := TOTAL_RE.search(body):
                    slot.total_ms = float(tm.group(1))
                    slot.total_tokens = int(tm.group(2))
                    continue
                continue                      # graphs reused, etc: noise
            if "release:" in body:
                rm = RELEASE_RE.search(body)
                slot.truncated = bool(rm and rm.group(2) != "0")
                slot.state = "idle"
                completions.append({
                    "slot": slot.id, "task": task,
                    "gen_tokens": slot.gen_tokens, "gen_tps": slot.gen_tps,
                    "prompt_tokens": slot.prompt_tokens, "prompt_tps": slot.prompt_tps,
                    "total_ms": slot.total_ms, "t": t, "truncated": slot.truncated,
                })
                if slot.truncated:
                    note("warn",
                         f"slot {slot.id} hit the context limit after {slot.gen_tokens} tokens "
                         f"(answer cut off — lower max tokens or use a bigger context)", t)
                continue
            continue

        # ------------------------------------- everything else: attention only
        # counts are derived from the attention list below, scoped to the window
        notable = next((msg for key, msg in NOTABLE if key in body), None)
        if level == "error":
            note("error", body, t)
        elif level == "warn":
            note("warn", notable or body, t)
        elif notable:
            note("warn", notable, t)

    if loading and loading_t is not None and wall - loading_t > _LOADING_MAX_AGE:
        loading = None       # stale evidence; stop claiming a load in progress

    # counters are scoped to the window the UI labels them with, so "2 warnings"
    # always means the same span of time as the chart sitting next to it
    horizon = wall - window_s
    req_window = [r for r in requests if r["t"] >= horizon]
    done_window = [c for c in completions if c["t"] >= horizon]
    errors = sum(1 for a in attention if a["level"] == "error" and wall - a["age_s"] >= horizon)
    warns = sum(1 for a in attention if a["level"] == "warn" and wall - a["age_s"] >= horizon)

    # the chart covers one window, not the whole journal tail: three hours of
    # 5-second bins would be 2000+ entries of payload for pixels that aren't there
    bins = {k: v for k, v in bins.items() if k >= horizon}
    pbins = {k: v for k, v in pbins.items() if k >= horizon}
    bins_out = []
    keys = set(bins) | set(pbins)     # a prefill-only window is still a window
    if keys:
        start = int(min(keys) // bin_s) * bin_s
        end = int(wall // bin_s) * bin_s
        b = start
        while b <= end:
            bins_out.append({"t": round(float(b), 1), "per": bins.get(b, {}),
                           "pr": pbins.get(b, {})})
            b += bin_s

    return {
        "slots": [s.as_dict(wall) for s in sorted(
            slots.values(), key=lambda s: (-(s.t_last or 0.0), s.id))],
        "active_slots": sum(1 for s in slots.values() if s.state in ("prompt", "generate")),
        "slot_ids": sorted({int(k) for per in bins.values() for k in per} | set(slots)),
        "series": {"bin_s": bin_s, "window_s": window_s, "bins": bins_out},
        "models": {
            "loaded": loaded,
            "loading": loading,
            "loading_age_s": round(wall - loading_t, 1) if (loading and loading_t) else None,
            "switching": bool(loading),
            "waiting_for_slot": waiting_for_slot if loading else False,
            "evictions": evictions,
        },
        "attention": _collapse(attention, wall)[-max_attention:][::-1],   # newest first
        "stats": {
            "window_s": window_s,
            "requests_window": len(req_window),
            "requests_per_min": round(len(req_window) / max(1.0, window_s / 60.0), 2),
            "tasks_done_window": len(done_window),
            "tokens_out_window": sum(c.get("gen_tokens") or 0 for c in done_window),
            "gen_tps_avg": round(sum(gen_rates) / len(gen_rates), 2) if gen_rates else None,
            "prompt_tps_avg": round(sum(prompt_rates) / len(prompt_rates), 1) if prompt_rates else None,
            "cache_keep_avg": round(sum(keeps) / len(keeps) * 100, 1) if keeps else None,
            "errors": errors,
            "warns": warns,
            "last_completion": done_window[-1] if done_window else None,
        },
        "time": {
            "now": round(wall, 1),
            "first_seen": round(prepared[0][0], 1) if prepared else None,
            "last_seen": round(prepared[-1][0], 1) if prepared else None,
            "wall_clock": has_epoch,
        },
        "counts": {"slots_seen": len(slots), "lines": len(prepared)},
    }
