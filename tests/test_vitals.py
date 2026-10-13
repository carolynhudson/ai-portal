"""The dashboard's vitals strip (§0.1 queue 40, docs/PORTAL_POLISH.md §1).

The operator's complaint, measured: the hero carries a **CPU line chart that is polled
constantly**, on a box where AI work pegs the GPU and leaves CPU idle — measured CPU 3.9%
while VRAM read 97% and the GTT pool 53%. A chart of the number that never moves is the
instrument that hides the one that does.

The requirement: **critical metrics visible, occasionally updated, lighting up when things get
dangerous.** Four tiles — GPU pool, VRAM, system RAM, disk — each with an `ok`/`watch`/`danger`
state from thresholds the operator can set, a last-sample stamp, and the line naming *who* is
using the pool. Not a 5-second line chart.

The tests are written against a synthetic snapshot, so the thresholds are checked at the
boundary values. A rule tested only on this box's live numbers is a rule that has never been
shown the boundary it is supposed to fire on.
"""
from __future__ import annotations

from app import vitals


def _gpu(**kw):
    base = {"available": True, "pool_total": 116 * 1024**3, "pool_used": 61 * 1024**3,
            "gtt_total": 116 * 1024**3, "gtt_used": 61 * 1024**3,
            "vram_total": 512 * 1024**2, "vram_used": 480 * 1024**2, "is_igpu": True}
    base.update(kw)
    return base


def _mem(**kw):
    base = {"total": 128 * 1024**3, "available": 53 * 1024**3, "used": 75 * 1024**3,
            "percent": 58.2}
    base.update(kw)
    return base


def _disk(**kw):
    base = {"root": {"total": 2 * 1024**4, "used": 700 * 1024**3, "free": 1300 * 1024**3,
                     "percent": 35.0},
            "ai": {"total": 4 * 1024**4, "used": 1400 * 1024**3, "free": 2600 * 1024**3,
                   "percent": 35.0}}
    base.update(kw)
    return base


def _snap(**kw):
    base = {"ts": 1000.0, "cpu_percent": 3.9, "load": 2.66,
            "memory": _mem(), "disk": _disk(), "gpu": _gpu()}
    base.update(kw)
    return base


# --- the tiles -----------------------------------------------------------------------------

def test_the_strip_is_four_tiles_in_the_operators_order():
    """The order is the operator's: the pool first, because on an iGPU the pool is the number
    that matters, and VRAM second as the number that *looks* alarming on this box by design."""
    strip = vitals.strip(_snap())
    assert [t["key"] for t in strip["tiles"]] == ["pool", "vram", "ram", "disk"]
    for t in strip["tiles"]:
        assert {"key", "label", "state", "used", "total", "percent", "at"} <= set(t), t


def test_a_tile_names_what_it_measures_and_when():
    """The strip is "occasionally updated", so a tile without a stamp is a number the operator
    cannot age. "Last sample 12:04" is the difference between a reading and a guess."""
    strip = vitals.strip(_snap(ts=1_700_000_000.0))
    for t in strip["tiles"]:
        assert t["at"] == 1_700_000_000.0, "every tile carries the same sample stamp"
    assert strip["at"] == 1_700_000_000.0


def test_the_pool_is_the_tile_that_carries_the_danger_on_an_igpu():
    """Measured on this box: VRAM 97% *by design* (a 512 MiB iGPU stub) while the pool the
    models load into sat at 53%. A strip that warns on VRAM here warns every day and means
    nothing; the pool is the number with the threshold that matters."""
    strip = vitals.strip(_snap())
    by = {t["key"]: t for t in strip["tiles"]}
    assert by["pool"]["state"] == "ok", "53% of the pool is not a warning"
    assert by["vram"]["percent"] > 90.0
    # VRAM near full on an iGPU is stated, not shouted: the operator's own reading of this box.
    assert by["vram"]["state"] in ("ok", "watch"), by["vram"]
    assert by["vram"].get("note"), "the iGPU stub is shown as a raw percentage with no context"


def test_the_thresholds_are_the_operators_and_fire_at_the_boundary():
    """Proposed defaults, from the plan: pool >=85% danger, >=70% watch; VRAM >=90% danger;
    RAM >=90% danger, >=75% watch; disk >=90% danger. Checked at the boundary on each side,
    because a threshold nobody tested at its boundary is a number, not a rule.

    The rungs: `watch` is *between* the watch value and the danger value; below the watch value is
    `ok`, and disk has no watch rung, so it goes `ok` then `danger`. A test that asked 69.9% of the
    pool to be `watch` was asserting a strip that lights up at *any* value under the danger line,
    which is not what "lighting up when things get dangerous" means.
"""
    cases = [
        ("pool", 69.9, "ok"), ("pool", 70.0, "watch"), ("pool", 84.9, "watch"),
        ("pool", 85.0, "danger"),
        ("ram", 74.9, "ok"), ("ram", 75.0, "watch"), ("ram", 89.9, "watch"),
        ("ram", 90.0, "danger"),
        ("disk", 89.9, "ok"), ("disk", 90.0, "danger"),
    ]
    for key, percent, want in cases:
        snap = _snap()
        # Kept in floats: a boundary asserted through int() truncation is a test of the
        # truncation, not of the threshold.
        if key == "pool":
            snap["gpu"]["pool_used"] = snap["gpu"]["pool_total"] * percent / 100.0
        elif key == "ram":
            # RAM percent is used/total, and the module reads `available`, so drive available.
            snap["memory"]["available"] = snap["memory"]["total"] * (100 - percent) / 100.0
        else:
            snap["disk"]["root"]["percent"] = percent
            snap["disk"]["ai"]["percent"] = percent
        by = {t["key"]: t for t in vitals.strip(snap)["tiles"]}
        assert by[key]["state"] == want, f"{key} at {percent}% -> {by[key]['state']}, want {want}"


def test_a_number_that_was_not_read_is_not_reported_as_safe():
    """`ok` means "measured, and inside the threshold". A tile for a metric that could not be
    read must never answer `ok`: on this box a missing amdgpu sysfs node and a healthy pool look
    identical in every earlier version of this code."""
    strip = vitals.strip(_snap(gpu=None))
    by = {t["key"]: t for t in strip["tiles"]}
    assert by["pool"]["state"] == "unknown", by["pool"]
    assert by["vram"]["state"] == "unknown", by["vram"]
    assert by["pool"]["percent"] is None
    assert by["ram"]["state"] == "ok", "the GPU is unreadable; RAM was read fine"

    # A disk that raised in the probe is None, not a zero.
    strip = vitals.strip(_snap(disk={"root": None, "ai": None}))
    by = {t["key"]: t for t in strip["tiles"]}
    assert by["disk"]["state"] == "unknown", by["disk"]


def test_the_worst_disk_is_the_disk_tile():
    """/home and ~/ai are separate filesystems on this box. A tile that reported the root's
    number would call a full ~/ai `ok`."""
    snap = _snap(disk={"root": {"total": 2 * 1024**4, "used": 100 * 1024**3, "free": 1900 * 1024**3,
                               "percent": 5.0},
                       "ai": {"total": 4 * 1024**4, "used": 3_700 * 1024**3,
                              "free": 300 * 1024**3, "percent": 91.0}})
    by = {t["key"]: t for t in vitals.strip(snap)["tiles"]}
    assert by["disk"]["state"] == "danger", "the tile must follow the worst filesystem"
    assert by["disk"]["label"] == "Disk (~/ai)", by["disk"]


def test_the_strip_names_who_is_using_the_pool():
    """The operator's own reading of the strip's purpose: "GPU pool at 53%" is answerable with
    "the 77.9 G model in slot 8080". The router census already names the slot and its model, so
    the line is assembled from facts the portal has, not from a probe."""
    # The shape the router report answers: a resident llama-server worker per model, with its
    # alias, port and RSS.
    servers = [
        {"alias": "llama-3.1-70b", "port": 8080, "rss_kib": 77 * 1024**2},
        {"alias": "embed-small", "port": 8081, "rss_kib": 2 * 1024**2},
    ]
    strip = vitals.strip(_snap(), servers=servers)
    assert strip["user"]["model"] == "llama-3.1-70b"
    assert strip["user"]["port"] == 8080
    assert "77" in strip["user"]["text"]
    # Nothing resident: the strip says so. "Nothing is loaded" is a fact, not an empty string.
    empty = vitals.strip(_snap(), servers=[])
    assert empty["user"]["text"], "a strip with no line is a strip that cannot answer its question"
    # A census that did not answer is reported as unknown, never as "nothing loaded".
    silent = vitals.strip(_snap(), servers=None)
    assert silent["user"]["state"] == "unknown", silent["user"]


def test_the_thresholds_are_portal_settings_so_the_operator_can_change_them():
    """The plan states it: thresholds that are portal settings (queue 41), not constants in the
    dashboard's script. The defaults are the operator's numbers."""
    keys = vitals.THRESHOLD_KEYS
    assert keys == ("pool_watch", "pool_danger", "vram_danger",
                    "ram_watch", "ram_danger", "disk_danger"), keys
    d = vitals.DEFAULTS
    assert d["pool_watch"] == 70.0 and d["pool_danger"] == 85.0
    assert d["vram_danger"] == 90.0
    assert d["ram_watch"] == 75.0 and d["ram_danger"] == 90.0
    assert d["disk_danger"] == 90.0


def test_an_operator_threshold_that_is_not_a_number_is_refused_not_ignored():
    """A settings page writes these values, so the reader has to refuse a bad one loudly. A
    threshold that silently fell back to the default would be an operator edit that did nothing."""
    bad = vitals.validate_thresholds({"pool_watch": "seventy"})
    assert bad["ok"] is False and "pool_watch" in bad["error"], bad
    bad = vitals.validate_thresholds({"pool_danger": 40.0, "pool_watch": 70.0})
    assert bad["ok"] is False, "danger below watch is not a threshold ladder"
    good = vitals.validate_thresholds({"pool_watch": 60.0, "pool_danger": 80.0})
    assert good["ok"] is True and good["value"]["pool_watch"] == 60.0


def test_a_partial_threshold_set_keeps_the_other_defaults():
    """The operator tunes one number. A reader that required all six, or that reset the rest to
    defaults, would make a single edit silently undo the others."""
    th = vitals.thresholds({"vram_danger": 50.0})
    assert th["vram_danger"] == 50.0
    assert th["pool_danger"] == vitals.DEFAULTS["pool_danger"]


def test_the_strip_never_carries_a_cpu_tile():
    """The whole point of the queue: the CPU chart is the wrong instrument on this workload
    (measured 3.9% while the pool ran hot). It is out of the hero; it does not come back as a
    tile."""
    strip = vitals.strip(_snap(cpu_percent=99.0))
    assert not [t for t in strip["tiles"] if "cpu" in t["key"].lower()], strip["tiles"]
