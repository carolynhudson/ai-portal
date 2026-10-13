"""The dashboard's vitals strip (§0.1 queue 40, docs/PORTAL_POLISH.md §1).

The operator's complaint, measured: the hero carried a **CPU line chart polled constantly**, on a
box where AI work pegs the GPU and leaves CPU idle — measured CPU 3.9% while VRAM read 97% and the
GTT pool 53%. A chart of the number that never moves is the instrument that hides the one that does.

The requirement, in their words: **critical metrics visible, occasionally updated, lighting up when
things get dangerous.** Four tiles in the operator's order — **pool, VRAM, RAM, disk** — each with an
`ok` / `watch` / `danger` state from thresholds the operator can set (queue 41), a last-sample stamp,
and the line naming *who* is using the pool. No CPU tile, at any threshold.

Two measurements shape this module.

**On an iGPU, VRAM is a stub and the pool is the number.** This box reports ~512 MiB of VRAM and it
is ~97% used *by design*; the memory the models load into is the GTT aperture (~116 GiB). A tile
that shouted on VRAM here shouted every day and meant nothing, so VRAM is stated with the reason it
looks full and the pool carries the danger.

**"Not read" is not "safe".** The health pane has been bitten by this twice: a missing amdgpu sysfs
node, a disk probe that raised, a router that did not answer. Every metric here has a fourth state,
`unknown`, and it is never collapsed into `ok`.
"""
from __future__ import annotations

from typing import Any

#: The thresholds the page can set (queue 41). Defaults are the operator's numbers, stated in
#: `docs/PORTAL_PLAN.md` §0.1 queue 40 and agreed on 2026-10-12.
DEFAULTS: dict[str, float] = {
    "pool_watch": 70.0,
    "pool_danger": 85.0,
    "vram_danger": 90.0,
    "ram_watch": 75.0,
    "ram_danger": 90.0,
    "disk_danger": 90.0,
}

#: The page lists them in this order, so the settings rows and the reader agree on one list.
THRESHOLD_KEYS: tuple[str, ...] = ("pool_watch", "pool_danger", "vram_danger",
                                   "ram_watch", "ram_danger", "disk_danger")

#: Watch is the rung below danger. A ladder whose danger rung sits below its watch rung is not a
#: ladder, and a settings page can be given exactly that.
_LADDER: dict[str, str] = {"pool_watch": "pool_danger", "ram_watch": "ram_danger"}

_STATE_RANK = {"ok": 0, "watch": 1, "danger": 2, "unknown": 1}


def validate_thresholds(overrides: Any) -> dict[str, Any]:
    """Can this dict be the threshold set? Answers with the reason, and never stores anything.

    The settings page writes these numbers, so a bad value is refused loudly: a threshold that
    quietly fell back to the default would be an operator edit that did nothing.
    """
    if overrides is None:
        return {"ok": True, "value": {}}
    if not isinstance(overrides, dict):
        return {"ok": False, "error": "thresholds must be an object of number values"}
    out: dict[str, float] = {}
    for key, raw in overrides.items():
        if key not in THRESHOLD_KEYS:
            return {"ok": False, "error": f"{key!r} is not a vitals threshold; the page sets "
                                          f"{', '.join(THRESHOLD_KEYS)}"}
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return {"ok": False, "error": f"{key} expects a number (a percentage between 0 and "
                                          f"100), got {raw!r}"}
        value = float(raw)
        if not 0.0 <= value <= 100.0:
            return {"ok": False, "error": f"{key} expects a percentage between 0 and 100, "
                                          f"got {value}"}
        out[key] = value
    for watch, danger in _LADDER.items():
        if watch in out and danger in out and out[danger] < out[watch]:
            return {"ok": False, "error": f"{danger} ({out[danger]}) is below {watch} "
                                          f"({out[watch]}); the danger rung has to sit above "
                                          f"the watch rung"}
    return {"ok": True, "value": out}


def thresholds(stored: Any = None) -> dict[str, float]:
    """The defaults, overlaid with whatever the operator stored.

    A partial set keeps every other default: the operator tunes one number, and a reader that
    reset the rest would make that edit silently undo the others. A stored value that cannot be
    read is reported on the strip rather than silently dropped — see `strip`.
    """
    out = dict(DEFAULTS)
    checked = validate_thresholds(stored)
    if checked["ok"]:
        out.update(checked["value"])
    return out


def _state(percent: float | None, watch: float | None, danger: float) -> str:
    if percent is None:
        return "unknown"
    if percent >= danger:
        return "danger"
    if watch is not None and percent >= watch:
        return "watch"
    return "ok"


def _gib(value: float | None) -> float | None:
    return None if value is None else round(value / (1024**3), 1)


def _pct(used: Any, total: Any) -> float | None:
    """used/total as a percentage, or None when either half was not read.

    `None` is the answer the tiles need: a metric that was not read is `unknown`, never 0%, and
    never `ok`. Truthiness alone would also call a legitimately-zero reading 0% -> unknown, so
    the check is `is None` on both halves.
    """
    if used is None or total is None or not total:
        return None
    return 100.0 * float(used) / float(total)


def _tile(key: str, label: str, state: str, used, total, percent, at,
          note: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "label": label, "state": state,
                           "used": used, "total": total, "percent": percent, "at": at}
    if note:
        out["note"] = note
    return out


def _threshold_state(snapshot: dict[str, Any]) -> tuple[dict[str, float], str | None]:
    """The thresholds for this strip, and the reason the operator's do not apply.

    Two sources, checked in order: `_thresholds` in the snapshot (the seam the pure tests use) and
    the portal settings the page writes. A stored value that cannot be read is reported on the
    strip — a silent fallback to defaults is an operator edit that did nothing, invisibly.
    """
    from .core import portalsettings

    if "_thresholds" in snapshot:
        checked = validate_thresholds(snapshot["_thresholds"])
        if not checked["ok"]:
            return dict(DEFAULTS), checked["error"]
        out = dict(DEFAULTS)
        out.update(checked["value"])
        return out, None
    return portalsettings.stored_thresholds()


def strip(snapshot: dict[str, Any], servers: list[dict] | None = None) -> dict[str, Any]:
    """The four tiles, from a metrics snapshot and the router's resident workers.

    `snapshot` is `app.metrics.snapshot()`'s shape; `servers` is the list of resident
    `llama-server` workers from `routerctl.self_report()` (`{"alias","port","rss_kib"}`), passed in
    so this stays a pure function of what it is given — the same discipline the health pane's
    advisories use, and the reason the boundary values are testable at all.
    """
    th, th_error = _threshold_state(snapshot)
    at = snapshot.get("ts")
    tiles: list[dict[str, Any]] = []

    # --- pool: the aperture the models load into (GTT on an iGPU, VRAM on a discrete card)
    gpu = snapshot.get("gpu")
    if not gpu:
        tiles.append(_tile("pool", "GPU pool", "unknown", None, None, None, at,
                           "no GPU was read, so the pool the models load into is unknown"))
        tiles.append(_tile("vram", "VRAM", "unknown", None, None, None, at,
                           "no GPU was read"))
    else:
        pool_pct = _pct(gpu.get("pool_used"), gpu.get("pool_total"))
        tiles.append(_tile("pool", "GPU pool", _state(pool_pct, th["pool_watch"], th["pool_danger"]),
                           _gib(gpu.get("pool_used")), _gib(gpu.get("pool_total")), pool_pct, at))
        vram_pct = _pct(gpu.get("vram_used"), gpu.get("vram_total"))
        # Measured: on an iGPU rocm-smi reports a ~512 MiB stub, so VRAM reads near full while the
        # pool sits at half. Shouting on it every day would train the operator to ignore the strip.
        vram_state = _state(vram_pct, None, th["vram_danger"])
        note = None
        if vram_state == "danger" and gpu.get("is_igpu"):
            vram_state = "watch" if pool_pct is not None and pool_pct < th["pool_danger"] else "danger"
            note = ("this box is an iGPU: the VRAM figure is the driver's ~512 MiB stub, not the "
                    "pool the models load into — the GPU pool tile is the one with the threshold "
                    "that matters")
        tiles.append(_tile("vram", "VRAM", vram_state,
                           _gib(gpu.get("vram_used")), _gib(gpu.get("vram_total")), vram_pct, at, note))

    # --- system RAM
    mem = snapshot.get("memory")
    ram_pct = None
    if mem and mem.get("total"):
        avail = mem.get("available")
        ram_pct = 100.0 * (mem["total"] - avail) / mem["total"] if avail is not None else None
    tiles.append(_tile("ram", "System RAM", _state(ram_pct, th["ram_watch"], th["ram_danger"]),
                       _gib((mem.get("total", 0) - mem.get("available", 0)) if mem and
                            mem.get("available") is not None else None),
                       _gib(mem.get("total")) if mem else None, ram_pct, at))

    # --- disk: the worst filesystem wins. `/` and `~/ai` are separate here, and a tile that
    # reported the root's number would call a full ~/ai `ok`.
    disks = snapshot.get("disk") or {}
    worst: tuple[float, str, dict] | None = None
    for label, path in (("Disk", "root"), ("Disk (~/ai)", "ai")):
        d = disks.get(path)
        if not d or not d.get("percent") and d.get("percent") != 0:
            continue
        if worst is None or d["percent"] > worst[0]:
            worst = (d["percent"], label, d)
    if worst is None:
        tiles.append(_tile("disk", "Disk", "unknown", None, None, None, at,
                           "no filesystem was read, so disk use is unknown"))
    else:
        percent, label, d = worst
        tiles.append(_tile("disk", label, _state(percent, None, th["disk_danger"]),
                           _gib(d.get("used")), _gib(d.get("total")), percent, at))

    # --- who is using it: the census names the slot, so "pool at 53%" is answerable with
    # "the 77.9 G model in slot 8080". Nothing resident is a fact; a census that did not
    # answer is not the same fact.
    if servers is None:
        user: dict[str, Any] = {"state": "unknown",
                                "text": "the router did not answer, so who is holding the pool "
                                        "is unknown"}
    elif not servers:
        user = {"state": "ok", "model": None, "port": None,
                "text": "no model is resident, so nothing is holding the pool"}
    else:
        top = max(servers, key=lambda s: s.get("rss_kib") or 0)
        gib = (top.get("rss_kib") or 0) / (1024**2)
        user = {"state": "ok", "model": top.get("alias") or top.get("model"),
                "port": top.get("port"), "rss_gib": round(gib, 1),
                "text": f"{top.get('alias') or top.get('model')} in slot {top.get('port')} "
                        f"holds {gib:.1f} G"}
    out: dict[str, Any] = {"tiles": tiles, "at": at, "user": user, "thresholds": th}
    if th_error:
        out["thresholds_error"] = th_error
    return out
