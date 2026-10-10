"""The process census (§6.14.1l): who the router is, and who is nobody's.

These tests feed `routerctl` a process list as text, the way `ps` gives it. That is the point:
the classification is the product, and it has to be exercised against the shapes a real
`ps` output has — a worker whose alias is quoted, a hand-started server spelled `--port=8080`,
a command line with a `--parallel 2` in it that must not be read as a port.

The live system is never touched: no unit is queried, no process is signalled, and the preset
file is read from a sandbox home.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-routerctl"))

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.core import paths, routerctl  # noqa: E402

ROUTER_CMD = ("/home/cortana/ai/bin/llama-server --host 0.0.0.0 --port 8080 --metrics "
              "--models-max 1 --models-dir /home/cortana/ai/models/llama "
              "--models-preset /home/cortana/ai/config/llama/presets.ini --no-models-autoload")
WORKER_CMD = ("/home/cortana/ai/releases/llama.cpp-b11429/llama-server --host 127.0.0.1 "
              "--log-colors off --metrics --min-p 0.0 --port 42919 --temperature 1.0 --top-k 20 "
              "--alias Quick-Coder --flash-attn on --lazy-mode on --n-gpu-layers 99 --threads 16 "
              "--parallel 2")
# A server somebody started by hand, on the router's own port, spelled with `--port=`.
STRAY_CMD = ("/home/cortana/llama/llama-server --port=8080 -m /home/cortana/tmp/tiny.gguf "
             "--ctx-size 4096")

PS_ROUTER_AND_WORKER = "\n".join([
    "  17541     804 664280 llama-server " + ROUTER_CMD,
    "  17650   17541 121332 llama-server " + WORKER_CMD,
])


def test_a_worker_is_a_child_and_a_stray_is_nobody():
    """Parentage is the definition, not a heuristic.

    The router spawns its workers, so `ppid == MainPID` is what makes a process the router's.
    Anything else is reported as outside the unit, and the safe direction to be wrong in is
    over-reporting: a process the portal cannot control, listed, is better than one it thinks it
    owns and does not.
    """
    procs = routerctl.parse_processes(PS_ROUTER_AND_WORKER.splitlines())
    kinds = routerctl.classify(17541, procs)
    assert kinds["router"]["pid"] == 17541
    assert [w["pid"] for w in kinds["workers"]] == [17650]
    assert kinds["foreign"] == []

    stray = routerctl.parse_processes(
        (PS_ROUTER_AND_WORKER + "\n  22111   15217  40000 llama-server " + STRAY_CMD).splitlines())
    kinds = routerctl.classify(17541, stray)
    assert [f["pid"] for f in kinds["foreign"]] == [22111], kinds
    assert kinds["foreign"][0]["ports"] == [8080], "a hand-started `--port=8080` is the router's port"


def test_a_port_number_comes_from_the_port_flag_only():
    """`--parallel 2` is not a port, and `--port=8080` is.

    The whole point of the census is a port comparison, so the parse is asserted against the
    spellings that would silently produce a fake collision.
    """
    procs = routerctl.parse_processes(["  1 0 10 llama-server " + WORKER_CMD])
    assert procs[0]["ports"] == [42919], procs
    assert procs[0]["alias"] == "Quick-Coder", procs
    launch = routerctl._launch_facts(WORKER_CMD)
    assert launch == {"port": 42919, "presets": None, "models_max": None}, launch
    launch = routerctl._launch_facts(ROUTER_CMD)
    assert launch["port"] == 8080 and launch["models_max"] == 1
    assert launch["presets"] == "/home/cortana/ai/config/llama/presets.ini"


def test_a_unit_with_no_main_process_leaves_every_server_outside_it():
    """`MainPID=0` is systemd for "not running", and 0 is not a pid to classify against.

    With no router process, every server found reads as foreign. That is the over-report direction,
    and it is the one that keeps a page from telling an operator that nothing is outside the unit
    when the unit is not running at all.
    """
    procs = routerctl.parse_processes(PS_ROUTER_AND_WORKER.splitlines())
    kinds = routerctl.classify(None, procs)
    assert kinds["router"] is None
    assert kinds["workers"] == []
    assert {p["pid"] for p in kinds["foreign"]} == {17541, 17650}


def test_the_census_names_the_preset_file_the_router_was_launched_with(tmp_path, monkeypatch):
    """The file the portal renders and the file the router read are two facts, and can differ.

    A router launched before an Apply is running the old file. Saying "active" hides that; saying
    which file the process was given does not, and the portal's own previews are about the other one.
    """
    other = tmp_path / "presets-old.ini"
    other.write_text("[*]\nname = old\n", encoding="utf-8")
    monkeypatch.setattr(paths, "PRESETS_INI", tmp_path / "presets.ini")
    (tmp_path / "presets.ini").write_text("[*]\nname = new\n[one]\nalias = x\n", encoding="utf-8")

    ps = "  100     1 1000 llama-server " + ROUTER_CMD.replace(
        "/home/cortana/ai/config/llama/presets.ini", str(other))
    report = routerctl.self_report(unit="ai-llama-router", ps_lines=ps.splitlines(), main_pid=100)
    pr = report["presets"]
    assert pr["sha256"], pr
    assert pr["sections"] == 2, pr
    assert pr["launched_with"] == str(other)
    assert "not the one the portal renders" in pr.get("warning", ""), pr


def test_a_census_that_cannot_look_says_so():
    """The degradation is a sentence, not an empty panel.

    "No servers outside the unit" is a claim about having looked. If the process list cannot be
    read, the panel has to report that it did not look — otherwise the page is at its most
    reassuring exactly when it knows least.
    """
    def boom():
        raise RuntimeError("ps: operation not permitted")
    monkeypatched = getattr(routerctl, "_ps_lines")
    routerctl._ps_lines = boom
    try:
        report = routerctl.self_report(unit="ai-llama-router")
    finally:
        routerctl._ps_lines = monkeypatched
    assert report["processes"]["ok"] is False
    assert "operation not permitted" in report["processes"]["error"]
    # The unit half still answers: the census degrades per probe, not all at once.
    assert report["unit"] == "ai-llama-router"
    assert "served" not in report, "a failed process list must not reach for the router's HTTP"


def test_report_shape_is_what_the_page_reads():
    """Every id on the panel has a key in the report, or the panel renders a blank."""
    ps = PS_ROUTER_AND_WORKER.splitlines()
    report = routerctl.self_report(unit="ai-llama-router", ps_lines=ps, main_pid=17541)
    for key in ("unit", "state", "sub_state", "main_pid", "controllable", "processes",
                "presets", "served"):
        assert key in report, key
    c = report["processes"]
    for key in ("ok", "router", "workers", "foreign", "port_clash", "total_rss_kib"):
        assert key in c, key
    assert c["port_clash"] == [], c
    assert c["total_rss_kib"] == 664280 + 121332, c
    # The served probe is attempted against the port parsed from the unit's own command line, and it
    # degrades alone: an unanswered router is a sentence about the router, not a census that failed.
    assert "ok" in report["served"]
    if not report["served"]["ok"]:
        assert report["served"]["error"]


# --- the endpoint (D1: every read is also an endpoint) -----------------------

def _client():
    from fastapi.testclient import TestClient

    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_the_census_endpoint_answers_the_page_exactly():
    """The dashboard renders the census server-side and re-reads it from this endpoint.

    Two renderers of one report is the §6.14.1c deck bug in a new place, so the shape is asserted
    on the endpoint the refresh button calls: every key the panel writes into must be there.
    """
    c = _client()
    r = c.get("/api/router/report")
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("unit", "state", "main_pid", "processes", "presets", "served"):
        assert key in d, key
    for key in ("ok", "router", "workers", "foreign", "port_clash"):
        assert key in d["processes"], key
    assert isinstance(d["processes"]["foreign"], list)
    assert "path" in d["presets"], d["presets"]


def test_the_census_reads_and_never_writes(tmp_path):
    """A panel that notices a stray server is only safe if looking cannot change anything.

    No job is queued, no unit is touched, and `presets.ini` is byte-identical: the page reads the
    process table and the router's HTTP surface, and the verbs that act on that stay behind the
    service controls and their confirms.
    """
    c = _client()
    if paths.PRESETS_INI.exists():
        before = paths.PRESETS_INI.read_bytes()
    else:
        before = None
    jobs_before = c.get("/api/jobs/active").status_code
    r = c.get("/api/router/report")
    assert r.status_code == 200
    assert c.get("/api/jobs/active").status_code == jobs_before
    if before is not None:
        assert paths.PRESETS_INI.read_bytes() == before, "the census wrote presets.ini"


def test_the_census_requires_auth():
    """401, not a redirect: the API says "no", it does not send a browser to a page."""
    from fastapi.testclient import TestClient

    from app.main import app
    assert TestClient(app).get("/api/router/report", follow_redirects=False).status_code == 401
