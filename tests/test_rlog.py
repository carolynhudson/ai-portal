"""Tests for the router log reader (app/core/rlog.py).

The fixtures are real journal lines from b10913 running in router mode (paths
anonymized), plus the rarer router messages taken verbatim from
llama.cpp's ``tools/server/server-models.cpp``. These tests are the reason the
dashboard can say "slot 1 is generating, 512 tokens, 6.0 t/s" instead of showing
a wall of nearly identical log lines.
"""
from __future__ import annotations

import pathlib

from app.core import rlog

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "router_journal.log"


def lines(*msgs: str) -> list[str]:
    return [m for m in msgs]


def stamp(sec: float, level: str = "I", body: str = "") -> str:
    """Render a line the way llama.cpp does: min.sec.ms.us + level + message."""
    mins, rest = divmod(float(sec), 60)
    secs, frac = divmod(rest, 1)
    ms = int(frac * 1000)
    us = int(round((frac * 1000 - ms) * 1000))
    return f"[55601] {int(mins):d}.{int(secs):02d}.{ms:03d}.{us:03d} {level} {body}"


# --------------------------------------------------------------------- basics

def test_reads_a_real_journal_tail():
    raw = FIXTURE.read_text().splitlines()
    pulse = rlog.parse(raw, window_s=7200, bin_s=10)
    assert pulse["counts"]["lines"] == len(raw)
    assert pulse["counts"]["slots_seen"] >= 2
    assert pulse["models"]["loaded"]                      # last proxied model wins
    # every reported slot has an id and a state we understand
    for slot in pulse["slots"]:
        assert slot["state"] in {"idle", "queued", "prompt", "generate"}
        assert isinstance(slot["id"], int)
    # throughput was measured, not invented
    assert pulse["stats"]["gen_tps_avg"] and pulse["stats"]["gen_tps_avg"] > 0
    assert pulse["stats"]["prompt_tps_avg"] > pulse["stats"]["gen_tps_avg"]


def test_empty_and_unparseable_input_degrade_cleanly():
    for raw in ([], [""], ["not a log line at all"], [None]):
        pulse = rlog.parse([x for x in raw if x] or [])
        assert pulse["slots"] == []
        assert pulse["active_slots"] == 0
        assert pulse["models"]["loaded"] is None
        assert pulse["series"]["bins"] == []
        assert pulse["stats"]["gen_tps_avg"] is None


# ---------------------------------------------------------------- per-slot state

def test_generation_is_attributed_to_its_slot():
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  1 | task 500 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  1 | task 500 | n_gen =  100, tg =   5.97 t/s, tg_3s =   6.02 t/s"),
        stamp(16, body="slot print_timing: id  1 | task 500 | n_gen =  512, tg =   6.10 t/s, tg_3s =   6.05 t/s"),
    ))
    assert pulse["active_slots"] == 1
    slot = pulse["slots"][0]
    assert (slot["id"], slot["state"]) == (1, "generate")
    assert slot["gen_tokens"] == 512                 # latest tick, not the first
    assert slot["gen_tps"] == 6.10
    assert slot["gen_tps_recent"] == 6.05
    assert slot["elapsed_s"] == 6.0                  # 13s..16s of a task started at 10s


def test_prompt_read_reports_real_progress():
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  3 | task 600 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  3 | task 600 | prompt processing, n_tokens = 779, "
                       "progress = 0.25, t = 3.53 s / 220.72 tokens per second"),
    ))
    slot = pulse["slots"][0]
    assert slot["state"] == "prompt"
    assert slot["prompt_pct"] == 0.25                # llama.cpp tells us; we don't fake it
    assert slot["prompt_done"] == 779
    assert slot["prompt_tps"] == 220.72


def test_a_slot_is_idle_again_after_release():
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  1 | task 700 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  1 | task 700 | n_gen =  100, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
        stamp(20, body="slot      release: id  1 | task 700 | stop processing: n_tokens = 900, truncated = 0"),
    ))
    slot = pulse["slots"][0]
    assert slot["state"] == "idle"
    assert pulse["active_slots"] == 0
    done = pulse["stats"]["last_completion"]
    assert done["gen_tokens"] == 100 and done["task"] == 700


def test_a_new_task_on_the_same_slot_resets_the_previous_one():
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  1 | task 10 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  1 | task 10 | n_gen =  900, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
        stamp(20, body="slot      release: id  1 | task 10 | stop processing: n_tokens = 900, truncated = 0"),
        stamp(30, body="slot launch_slot_: id  1 | task 11 | processing task, is_child = 0"),
        stamp(33, body="slot print_timing: id  1 | task 11 | n_gen =    12, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
    ))
    slot = pulse["slots"][0]
    assert slot["task"] == 11 and slot["gen_tokens"] == 12
    assert slot["elapsed_s"] == 3.0                  # of the new task, not 23s


def test_truncation_is_reported_as_an_attention_item():
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  2 | task 20 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  2 | task 20 | n_gen =  400, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
        stamp(15, body="slot      release: id  2 | task 20 | stop processing: n_tokens = 8192, truncated = 1"),
    ))
    assert pulse["slots"][0]["truncated"] is True
    texts = [a["text"] for a in pulse["attention"]]
    assert any("context limit" in t for t in texts)


def test_a_busy_slot_that_goes_quiet_is_marked_stalled_not_busy():
    """A running slot prints a timing line every ~3s. Silence while still marked
    busy means it is waiting on something outside itself - say so."""
    quiet = lines(
        stamp(10, body="slot launch_slot_: id  1 | task 5 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  1 | task 5 | n_gen =  10, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
    )
    talking = rlog.parse(quiet, now_wall=14.0)
    assert talking["slots"][0]["stalled"] is False
    silent = rlog.parse(quiet, now_wall=120.0)
    assert silent["slots"][0]["stalled"] is True
    assert silent["slots"][0]["age_s"] == 107.0


def test_a_new_task_does_not_inherit_the_previous_tasks_speed():
    pulse = rlog.parse(lines(
        stamp(10, body="slot print_timing: id  1 | task 4 | n_gen =  900, tg =  20.00 t/s, tg_3s =  20.00 t/s"),
        stamp(30, body="slot launch_slot_: id  1 | task 5 | processing task, is_child = 0"),
    ))
    slot = pulse["slots"][0]
    assert slot["state"] == "prompt" and slot["gen_tokens"] == 0
    assert slot["gen_tps"] is None                 # 20 t/s belonged to the old task


# ---------------------------------------------------------------- model state

def test_a_model_switch_shows_as_loading():
    pulse = rlog.parse(lines(
        stamp(100, body="srv  proxy_reques: proxying request to model Mistral-Code-24B on port 55601"),
        stamp(101, body="model name=Mistral-Code-24B is not loaded, loading..."),
        stamp(115, body="waiting until model name=Mistral-Code-24B is fully loaded..."),
    ))
    assert pulse["models"]["loaded"] == "Mistral-Code-24B"
    assert pulse["models"]["loading"] == "Mistral-Code-24B"
    assert pulse["models"]["switching"] is True
    assert pulse["models"]["waiting_for_slot"] is True    # queued behind running work
    assert pulse["models"]["loading_age_s"] == 14.0


def test_a_request_line_alone_does_not_mean_the_model_is_live():
    """`proxying request` is what *triggers* a load - clearing `loading` on it would
    hide the switch exactly while the user is waiting for it."""
    pulse = rlog.parse(lines(
        stamp(100, body="model name=Big-Model is not loaded, loading..."),
        stamp(101, body="srv  proxy_reques: proxying request to model Big-Model on port 55601"),
    ))
    assert pulse["models"]["loading"] == "Big-Model"


def test_slot_activity_proves_the_model_is_loaded():
    pulse = rlog.parse(lines(
        stamp(100, body="model name=Big-Model is not loaded, loading..."),
        stamp(140, body="slot launch_slot_: id  0 | task 88 | processing task, is_child = 0"),
        stamp(143, body="slot print_timing: id  0 | task 88 | n_gen =   10, tg =   9.00 t/s, tg_3s =   9.00 t/s"),
    ))
    assert pulse["models"]["loading"] is None
    assert pulse["active_slots"] == 1


def test_eviction_is_counted_and_shown():
    pulse = rlog.parse(lines(
        stamp(100, body="evicting idle LRU name=Qwen3.8-Flash-Next-GGUF for a queued request"),
    ))
    assert pulse["models"]["evictions"] == 1
    assert any("evicting Qwen3.8" in a["text"] for a in pulse["attention"])


def test_stale_load_evidence_stops_being_reported():
    pulse = rlog.parse(lines(
        stamp(10, body="model name=Ghost-Model is not loaded, loading..."),
        stamp(5000, body="slot print_timing: id  0 | task 900 | n_gen =  1, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
    ))
    # load evidence is hours old and the router has been busy since: don't claim it
    assert pulse["models"]["loading"] is None


# ------------------------------------------------------------------- attention

def test_the_chatter_is_dropped_and_the_signal_kept():
    pulse = rlog.parse(lines(
        stamp(10, body="slot print_timing: id  1 | task 5 |    graphs reused =   291876"),
        stamp(11, body="slot get_availabl: id  1 | task -1 | selected slot by LCP similarity, "
                       "f_sim_best = 0.996 (> 0.100 thold), f_keep = 1.000"),
        stamp(12, body="find_slot: non-consecutive token position 513 after 511 for sequence 4"),
        stamp(13, body="srv alloc: 0 - making room for prompt cache entry, removing oldest entry (size = 32.0 MiB)"),
        stamp(14, level="E", body="srv operator(): http client error: Connection handling canceled"),
    ))
    texts = " | ".join(a["text"] for a in pulse["attention"])
    assert "graphs reused" not in texts
    assert "position mismatch" in texts          # translated, not just echoed
    assert "prompt cache pressure" in texts
    assert "http client error" in texts
    assert pulse["stats"]["errors"] == 1


def test_repeated_notices_collapse_into_one_line_with_a_count():
    """Cache pressure fires dozens of times a minute; ten copies of one sentence
    is the wall of look-alike lines this pane exists to remove."""
    raw = [stamp(100 + i * 3, level="W",
                 body="srv alloc: 0 - making room for prompt cache entry, removing oldest entry (size = 32.0 MiB)")
          for i in range(9)]
    raw.append(stamp(130, level="W", body="srv stop: cancel task, id_task = 7"))
    pulse = rlog.parse(raw, window_s=3600)
    items = pulse["attention"]
    assert len(items) == 2
    assert items[0]["text"].startswith("a request was cancelled")   # newest first
    pressure = items[1]
    assert pressure["count"] == 9
    assert pressure["age_s"] == 6.0                # age of the newest occurrence


def test_attention_is_newest_first_and_window_scoped_counts():
    pulse = rlog.parse(lines(
        stamp(10, level="W", body="srv stop: cancel task, id_task = 1"),
        stamp(11, level="W", body="out of memory while allocating batch"),
        stamp(4000, level="W", body="srv stop: cancel task, id_task = 2"),
    ), window_s=600)
    assert [a["text"] for a in pulse["attention"]] == [
        "a request was cancelled before it finished",      # newest first
        "out of memory",
    ]
    assert [a["age_s"] for a in pulse["attention"]] == [0.0, 3989.0]
    assert pulse["stats"]["warns"] == 1                   # only the in-window one
    assert pulse["stats"]["warns"] == 1           # only the one inside the window


def test_cache_reuse_is_averaged_from_lcp_similarity():
    pulse = rlog.parse(lines(
        stamp(10, body="slot get_availabl: id  1 | task -1 | selected slot by LCP similarity, f_keep = 1.000"),
        stamp(11, body="slot get_availabl: id  2 | task -1 | selected slot by LCP similarity, f_keep = 0.500"),
    ))
    assert pulse["stats"]["cache_keep_avg"] == 75.0


def test_durations_are_formatted_once_on_the_server():
    """Template and browser both read these strings, so they cannot drift apart."""
    pulse = rlog.parse(lines(
        stamp(10, body="slot launch_slot_: id  1 | task 5 | processing task, is_child = 0"),
        stamp(13, body="slot print_timing: id  1 | task 5 | n_gen =  10, tg =   5.00 t/s, tg_3s =   5.00 t/s"),
        stamp(200, level="W", body="srv stop: cancel task, id_task = 4"),
    ))
    slot = pulse["slots"][0]
    assert slot["elapsed_text"] == "3s"
    assert slot["age_text"] == "3m 7s"        # last word from this slot, 187s ago
    assert pulse["attention"][0]["age_text"] == "0s"


# ------------------------------------------------------------------ time series

def test_bins_are_five_second_slots_per_slot_and_capped_to_the_window():
    raw = []
    for sec in range(0, 130, 3):
        raw.append(stamp(sec, body=f"slot print_timing: id  1 | task 5 | n_gen =  {sec}, tg =   6.00 t/s, tg_3s =   6.00 t/s"))
        raw.append(stamp(sec, body=f"slot print_timing: id  3 | task 6 | n_gen =  {sec}, tg =   2.00 t/s, tg_3s =   2.00 t/s"))
    pulse = rlog.parse(raw, window_s=60, bin_s=5)
    bins = pulse["series"]["bins"]
    assert len(bins) <= 14                       # ~60s of 5s bins, not 130s worth
    assert bins[-1]["per"].keys() <= {"1", "3"}
    totals = [sum(b["per"].values()) for b in bins if b["per"]]
    assert any(t > 7 for t in totals)            # both slots contributing
    assert pulse["slot_ids"] == [1, 3]


def test_prompt_rates_bin_on_their_own_channel_beside_generation():
    # prefill prints its rate on the same print_timing cadence; the chart
    # needs it time-stamped on the same grid, on its own scale
    raw = [
        stamp(10, body="slot launch_slot_: id  1 | task 5 | processing task"),
        stamp(12, body="slot print_timing: id  1 | task 5 | prompt processing, n_tokens =   1024, progress = 0.25, t =   4.00 s / 256.00 tokens per second"),
        stamp(16, body="slot print_timing: id  1 | task 5 | prompt processing, n_tokens =   4096, progress = 1.00, t =  16.00 s / 256.00 tokens per second"),
        stamp(20, body="slot print_timing: id  1 | task 5 | prompt eval time = 16000.00 ms / 4096 tokens (256.00 tokens per second, 3.91 ms per token)"),
        stamp(24, body="slot print_timing: id  1 | task 5 | n_gen =  128, tg =   6.00 t/s, tg_3s =   6.00 t/s"),
    ]
    pulse = rlog.parse(raw, window_s=60, bin_s=5, now_wall=30.0)
    bins = pulse["series"]["bins"]
    prs = [b for b in bins if b.get("pr")]
    assert prs, "prompt rates never reached the bins"
    assert all(v == 256.0 for b in prs for v in b["pr"].values())
    assert any(b["pr"] and not b["per"] for b in bins), "prefill-only bins missing"
    assert any(b["per"] for b in bins), "generation bins missing"
    # prefill ending and generation starting inside one 5 s bin land in the
    # same bin, on their separate channels: that is the real transition time
    both = [b for b in bins if b["pr"] and b["per"]]
    assert both and all(b["pr"] == {"1": 256.0} for b in both)


def test_wall_clock_ages_when_the_journal_supplies_them():
    epoch = 1_700_000_000.0
    pairs = [
        (epoch - 40, stamp(100, body="slot launch_slot_: id  1 | task 9 | processing task, is_child = 0")),
        (epoch - 10, stamp(130, body="slot print_timing: id  1 | task 9 | n_gen =  40, tg =   4.00 t/s, tg_3s =   4.00 t/s")),
    ]
    pulse = rlog.parse(pairs, now_wall=epoch)
    assert pulse["time"]["wall_clock"] is True
    assert pulse["slots"][0]["age_s"] == 10.0     # 10 seconds ago, not "just now"
    assert pulse["slots"][0]["elapsed_s"] == 30.0


def test_malformed_lines_inherit_the_previous_stamp_instead_of_crashing():
    pulse = rlog.parse(lines(
        stamp(50, body="slot launch_slot_: id  1 | task 3 | processing task, is_child = 0"),
        "continuation without a timestamp",
    ))
    assert pulse["counts"]["lines"] == 2
    assert pulse["slots"][0]["state"] == "prompt"
