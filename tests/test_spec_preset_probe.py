"""The preset probe's two accounting channels.

Stage 3 of the MTP question — does a per-model preset section actually make the worker draft —
is answered by numbers, so the numbers are the thing worth pinning. Both channels were broken
when first written: /metrics was read without --metrics (the endpoint answers "start it with
`--metrics`"), then parsed under names llama.cpp never publishes (llama:spec_drafts_total, then
the unprefixed names — the Prometheus writer prefixes "llamacpp:"), and the request never named
a model, which a multi-model router refuses with 400. Each defect showed up as a None and read as
"the draft did nothing".
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _probe():
    path = ROOT / "tools" / "spec_preset_probe.py"
    spec = importlib.util.spec_from_file_location("spec_preset_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["spec_preset_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe():
    return _probe()


@pytest.fixture()
def router(probe, tmp_path):
    """A Router that never starts a server: only the readers are under test."""
    r = probe.Router.__new__(probe.Router)
    r.port = 1
    r.log = tmp_path / "worker.log"
    return r


# a worker log trimmed to the two lines that carry the verdict, copied verbatim from a real
# b11146 run (Qwopus3.5-4B-Coder Q8_0, spec-type carried by a preset section)
WORKER_LOG = """
[34901] 0.14.816.269 I slot print_timing: id  3 | task 0 | draft acceptance = 0.81481 (   44 accepted /    54 generated), mean len =  3.44
[34901] 0.14.816.319 I spec common_specu: statistics        draft-mtp: #calls(b,g,a) =    1     18     18, #gen drafts =     18, #acc drafts =     17, #gen tokens =     54, #acc tokens =     44
"""


def test_the_worker_log_line_carries_the_acceptance_verdict(probe, router):
    router.log.write_text(WORKER_LOG)
    got = router.log_acceptance()
    assert got["log_accept_rate"] == pytest.approx(0.81481)
    assert (got["log_accepted"], got["log_generated"]) == (44, 54)
    stats = got["log_stats"]
    assert stats == {"gen_drafts": 18, "acc_drafts": 17, "gen_tokens": 54, "acc_tokens": 44}


def test_a_run_that_never_drafted_reports_nothing_rather_than_zero(probe, router):
    """Absence and zero are different answers; a log with no drafting must not read as either."""
    router.log.write_text("0.00.1 I srv operator(): available models (1):\n")
    assert router.log_acceptance() == {}


def test_the_counter_names_are_the_names_the_deployed_build_emits(probe):
    """Pinned against the source, because two guesses have already been shipped.

    server-task.cpp builds the counters as bare names and the writer prefixes them "llamacpp:";
    a probe parsing "llama:spec_drafts_total" finds nothing and reports None for every key.
    """
    src = pathlib.Path("/home/cortana/ai/llama.cpp/tools/server/server-task.cpp")
    if not src.is_file():
        pytest.skip("no llama.cpp source checkout on this machine")
        return
    text = src.read_text()
    for name in probe.SPEC_METRICS.values():
        assert name.split(":", 1)[1] in text, f"{name} is not a metric this build defines"
    assert all(n.startswith("llamacpp:") for n in probe.SPEC_METRICS.values())


def test_metrics_are_read_from_the_worker_the_router_proxies_to(probe, router, monkeypatch):
    """/metrics on a router is proxied to a worker, and a request that names no model is refused.

    The bare path is kept only as the fallback it is: single-model case.
    """
    seen = []

    def fake_get(url, timeout=20):
        seen.append(url)
        return (
            "# HELP llamacpp:spec_decode_num_drafts_total Speculative: Total drafts\n"
            "llamacpp:spec_decode_num_drafts_total 18\n"
            "llamacpp:spec_decode_num_draft_tokens_total 54\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 44\n"
        )

    monkeypatch.setattr(probe, "get", fake_get)
    vals = router.spec_counters("org/Repo-GGUF:Q8_0")
    assert seen[0].endswith("/metrics?model=org/Repo-GGUF:Q8_0")
    assert vals == {"drafts": 18.0, "draft_tok": 54.0, "accepted_tok": 44.0}


def test_an_unreadable_metrics_endpoint_says_so_instead_of_reporting_nothing(
    probe, router, monkeypatch
):
    """Without --metrics the endpoint errors; that is a missing instrument, not a zero draft."""

    def fake_get(url, timeout=20):
        raise RuntimeError("metrics endpoint not supported")

    monkeypatch.setattr(probe, "get", fake_get)
    vals = router.spec_counters("org/Repo-GGUF:Q8_0")
    assert "error" in vals and "metrics endpoint not supported" in vals["error"]


def test_a_metrics_body_with_no_spec_counters_is_reported_as_such(probe, router, monkeypatch):
    """Names change between builds; a body that parses but holds none must not read as zero."""
    monkeypatch.setattr(probe, "get", lambda url, timeout=20: "llamacpp:requests_total 3\n")
    vals = router.spec_counters("org/Repo-GGUF:Q8_0")
    assert vals["drafts"] is None and "error" in vals


# ---------------------------------------------------------------- the file under test
def test_a_per_model_section_is_keyed_by_the_id_the_router_lists(probe):
    """Default section == model id, so the section and the request name the same model."""
    text = probe.ini_text(["org/Repo-GGUF:Q8_0"], None, "model", "spec-type", "draft-mtp", 8192)
    assert "[org/Repo-GGUF:Q8_0]\nspec-type = draft-mtp" in text
    assert "spec-type" not in text.split("[", 2)[1].split("[")[0]   # not in [*]


def test_a_global_scope_puts_the_key_in_the_floor_section(probe):
    text = probe.ini_text(["org/Repo-GGUF:Q8_0"], None, "global", "offline", "true", 8192)
    head = text.split("[", 2)[1]
    assert "offline = true" in head


def test_a_deliberate_section_mismatch_is_still_testable(probe):
    """--section names the section something else — that is the alias-spelling question."""
    text = probe.ini_text(["org/Repo-GGUF:Q8_0"], ["Alias-Name"], "model",
                          "spec-type", "draft-mtp", 8192)
    assert "[Alias-Name]" in text and "[org/Repo-GGUF:Q8_0]" not in text
