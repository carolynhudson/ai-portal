"""The harness host (queue 30): the host refuses to shadow, and the portal knows who it is talking to.

Two measured facts drive these tests.

1. A second `opencode serve` on a busy port starts "successfully" and every request is answered by
   the **first** server. So the unit must refuse a busy port, and `status()` must name the process
   that owns it — a shadowed host is invisible otherwise.
2. Sessions live server-side (`~/.local/share/opencode/opencode.db`), and the project directory is a
   **query parameter** (`?directory=`); a body key with the same name is ignored — measured against
   1.19.0, where it put the session in the server's own cwd.

Nothing here touches the live system: no unit is queried, no request leaves the process, and the
secret is written to a sandbox home.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import types
from pathlib import Path

import pytest

os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-harnessctl"))

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import harnessctl, paths  # noqa: E402

# conftest pins AI_PORTAL_HOME to a throwaway tree before app.core.paths resolves, so
# paths.PORTAL_STATE is already sandbox-scoped. These tests clear the secret between
# cases; none of them touches the operator's real ~/ai.
PROJ = str(Path(tempfile.gettempdir()) / "harness-proj")
PW_KEY = "OPENCODE_" + "SERVER_PASSWORD"


def _fresh_state():
    paths.PORTAL_STATE.mkdir(parents=True, exist_ok=True)
    k = paths.PORTAL_STATE / harnessctl.KEY_FILE_NAME
    if k.exists():
        k.unlink()


def _cache(port: int = 4096) -> Path:
    """The cache home the host is started with — the directory it reads its config from."""
    return harnessctl.cache_dir(port)


# --- the secret ---------------------------------------------------------------------

def test_key_is_generated_once_and_is_not_world_readable():
    _fresh_state()
    first = harnessctl.ensure_key(log=lambda *a: None)
    assert first and harnessctl.key_present()
    assert harnessctl.key_path().stat().st_mode & 0o077 == 0
    # A second call never rotates the password: the running server would answer 401.
    assert harnessctl.ensure_key(log=lambda *a: None) == first


def test_the_client_refuses_without_a_key_and_says_where_the_host_is():
    _fresh_state()
    try:
        harnessctl.list_sessions()
    except harnessctl.HarnessError as e:
        assert "harness password" in str(e)
        assert "harness.key" in str(e)
    else:
        raise AssertionError("the client sent a request with no shared password")


def test_the_env_file_carries_the_secret_and_the_unit_does_not():
    _fresh_state()
    probe_value = "probe-" + "value-1"
    mapping = harnessctl.env_mapping(4096, probe_value)
    assert mapping["HARNESS_PORT"] == "4096"
    assert mapping[PW_KEY] == probe_value
    unit = harnessctl.unit_text()
    assert "ai-harness.service" not in unit      # the unit names the unit, not itself
    assert "ai-harness" in unit
    assert probe_value not in unit               # the secret never reaches the unit file
    assert "EnvironmentFile" in unit and "ai-harness.sh" in unit


# --- the busy port is refused, not shadowed -----------------------------------------

def test_port_free_answers_for_a_port_we_are_actually_holding():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert harnessctl.port_free(port) is False
        assert harnessctl.port_free(port + 1000 if port + 1000 < 65535 else port - 1000) is True
    finally:
        s.close()


def test_preflight_refuses_a_busy_port_with_the_reason_on_stderr(capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert harnessctl.preflight(port) == 1
        err = capsys.readouterr().err
        assert f"port {port} in use" in err
    finally:
        s.close()
    assert harnessctl.preflight(65500) == 0


def test_the_wrapper_says_what_a_shadowed_host_would_do():
    body = harnessctl.wrapper_body()
    assert "refusing to start" in body
    assert "first server" in body
    # The refusal runs before exec: a wrapper that checks and then execs anyway is a no-op.
    assert body.index("preflight") < body.index("exec ")


SS_BUSY = """State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
LISTEN 0 512 127.0.0.1:4096 0.0.0.0:* users:(("opencode",pid=223971,fd=17))
"""
SS_FREE = """State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
"""


def test_listener_reads_the_owner_out_of_ss(monkeypatch):
    def fake_run(argv, **kw):
        class R:
            stdout = SS_BUSY if "4096" in " ".join(argv) else SS_FREE
        return R()
    monkeypatch.setattr(harnessctl.subprocess, "run", fake_run)
    got = harnessctl.listener(4096)
    assert got and got["pid"] == 223971
    assert harnessctl.listener(9999) is None


def test_status_names_a_shadowed_host_instead_of_calling_it_healthy(monkeypatch):
    """The artifact, in the portal's own words: pid 223971 holds the port, the unit's MainPID is
    111, so whatever answers 4096 is not the host the portal started."""
    _fresh_state()
    monkeypatch.setattr(harnessctl, "unit_state",
                        lambda unit="ai-harness": {"unit": unit, "active": "active",
                                                   "sub": "running", "main_pid": 111,
                                                   "enabled": True})
    monkeypatch.setattr(harnessctl, "listener",
                        lambda port: {"pid": 223971, "line": "users:((\"opencode\",pid=223971))"})
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, host="127.0.0.1", password=None, timeout=6.0:
                        {"ok": True, "version": "1.19.0", "auth": "yes", "url": "x"})
    st = harnessctl.status(4096)
    assert st["shadowed"] is True
    assert "223971" in st["note"] and "ai-harness" in st["note"]
    assert st["unit"]["main_pid"] == 111


def test_status_says_auth_failed_loudly_rather_than_down(monkeypatch):
    _fresh_state()
    monkeypatch.setattr(harnessctl, "unit_state",
                        lambda unit="ai-harness": {"unit": unit, "active": "active",
                                                   "sub": "running", "main_pid": 5,
                                                   "enabled": True})
    monkeypatch.setattr(harnessctl, "listener", lambda port: {"pid": 5, "line": "x"})
    monkeypatch.setattr(harnessctl, "read_key", lambda: None)
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, host="127.0.0.1", password=None, timeout=6.0:
                        {"ok": True, "auth": "auth_failed", "url": "x"})
    st = harnessctl.status(4096)
    assert st["shadowed"] is False
    assert st["key_present"] is False
    assert "password" in st["note"]


def test_a_junk_mainpid_from_systemd_does_not_crash_the_read():
    """systemd answers `MainPID=0` for an inactive unit; a unit file with a weird value must
    not turn a status read into a traceback."""
    def fake_run(argv, **kw):
        class R:
            stdout = "MainPID=nonsense\nActiveState=inactive\n"
        return R()
    orig = harnessctl.subprocess.run
    harnessctl.subprocess.run = fake_run
    try:
        assert harnessctl.unit_state("ai-harness")["main_pid"] == 0
    finally:
        harnessctl.subprocess.run = orig


# --- the client: the measured request shapes ----------------------------------------

def test_create_session_puts_the_directory_in_the_query_not_the_body(monkeypatch):
    """Measured: a body key named `directory` is ignored and the session lands in the
    server's own working directory. The query parameter is the only shape that works."""
    seen = {}

    def fake_request(method, path, body=None, port=4096, query=None, timeout=20):
        seen["method"], seen["path"], seen["body"], seen["query"] = method, path, body, query
        return 200, {"id": "ses_1", "directory": "/home/x/proj"}
    monkeypatch.setattr(harnessctl, "_request", fake_request)
    out = harnessctl.create_session("/home/x/proj")
    assert out["id"] == "ses_1"
    assert seen["method"] == "POST" and seen["path"] == "/session"
    assert seen["query"] == {"directory": "/home/x/proj"}
    assert "directory" not in (seen["body"] or {})


def test_list_sessions_filters_by_cwd_and_refuses_a_non_list(monkeypatch):
    monkeypatch.setattr(harnessctl, "_request",
                        lambda *a, **k: (200, [{"id": "ses_1"}]))
    assert harnessctl.list_sessions(PROJ) == [{"id": "ses_1"}]
    monkeypatch.setattr(harnessctl, "_request", lambda *a, **k: (500, "boom"))
    try:
        harnessctl.list_sessions(PROJ)
    except harnessctl.HarnessError as e:
        assert "500" in str(e)
    else:
        raise AssertionError("a refused session list was returned as success")


def test_the_client_never_leaves_loopback():
    """The bandit audit on urlopen is about scheme freedom. The client builds every URL from
    `base()`, and this pins that: a host setting of `file` or of a remote name must not be
    enough to send the portal's password somewhere."""
    _fresh_state()
    harnessctl.ensure_key(log=lambda *a: None)
    for url in ("file:///etc/passwd", "https://example.com/session",
                "http://10.0.0.1:4096/session"):
        try:
            harnessctl._local_request(url)
        except harnessctl.HarnessError:
            pass
        else:
            raise AssertionError(f"the client accepted {url}")
    req = harnessctl._local_request("http://127.0.0.1:4096/session")
    assert req.full_url.endswith("/session")


def test_an_unanswered_host_names_the_unit_to_check(monkeypatch):
    _fresh_state()
    harnessctl.ensure_key(log=lambda *a: None)

    def boom(req, timeout=20):
        raise OSError("connection refused")
    monkeypatch.setattr(harnessctl.urllib.request, "urlopen", boom)
    try:
        harnessctl.list_sessions(PROJ)
    except harnessctl.HarnessError as e:
        msg = str(e)
        assert "did not answer" in msg
        assert "systemctl --user status ai-harness" in msg
    else:
        raise AssertionError("an unreachable host was reported as a successful list")


def test_prompt_sends_the_opencode_message_shape(monkeypatch):
    seen = {}

    def fake_request(method, path, body=None, port=4096, query=None, timeout=20):
        seen.update(method=method, path=path, body=body, query=query)
        return 200, {"info": {"id": "msg_1"}}
    monkeypatch.setattr(harnessctl, "_request", fake_request)
    out = harnessctl.prompt("ses_1", "hello", PROJ)
    assert out["info"]["id"] == "msg_1"
    assert seen["path"] == "/session/ses_1/message"
    assert seen["body"] == {"parts": [{"type": "text", "text": "hello"}]}
    assert seen["query"] == {"directory": PROJ}


# --- the host is seeded with the agent config it reads (queue 40, plan §6.14.1o) ---
#
# The gate measured that a session created over the API reads `opencode.json` and
# `AGENTS.md` out of the cache home the host is started with (`XDG_CACHE_HOME`), and
# that a host started with an empty cache home answers the model question from the
# router alone — it never sees the agent's posture, compaction, or plugins. Seeding
# is the step the portal owns, so it belongs in `start()`, and the page has to be
# able to tell the operator which of the two the host is running with.

def test_start_seeds_the_hosts_cache_home_before_the_unit_starts(monkeypatch):
    """Order matters: a cache home seeded *after* the host starts teaches a host that has
    already read an empty directory."""
    _fresh_state()
    home = _cache(4096)
    if home.exists():
        shutil.rmtree(home)
    # The agent config the seed copies from.
    oc = paths.HOME / ".config" / "opencode"
    oc.mkdir(parents=True, exist_ok=True)
    (oc / "opencode.json").write_text(json.dumps({"model": "x"}))
    (oc / "AGENTS.md").write_text("# posture\n")

    seen = {}

    def fake_unit(port, log=print):
        # What the host's cache home holds at the moment the unit is asked to start.
        seen["seeded_at_start"] = (home / "opencode.json").exists()
        seen["seeded_posture_at_start"] = (home / "AGENTS.md").exists()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(harnessctl, "port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(harnessctl.units, "write_env", lambda *a, **k: None)
    monkeypatch.setattr(harnessctl.units, "write_wrapper", lambda *a, **k: None)
    monkeypatch.setattr(harnessctl.units, "unit_path", lambda name: home / "unit")
    monkeypatch.setattr(harnessctl, "start_unit", fake_unit)
    monkeypatch.setattr(harnessctl.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(harnessctl, "status", lambda port=4096: {"ok": True, "port": port})
    out = harnessctl.start(4096)
    assert out["ok"] is True
    assert seen.get("seeded_at_start") is True, "the config must be in place before the host starts"
    assert seen.get("seeded_posture_at_start") is True


def test_start_refuses_a_host_it_cannot_seed(monkeypatch):
    """A host with nothing to read is a host answering from defaults. The portal says so
    and refuses, rather than starting a host the page would call ready."""
    _fresh_state()
    home = _cache(4096)
    if home.exists():
        shutil.rmtree(home)
    if (paths.HOME / ".config" / "opencode" / "opencode.json").exists():
        (paths.HOME / ".config" / "opencode" / "opencode.json").unlink()
    monkeypatch.setattr(harnessctl, "port_free", lambda port, host="127.0.0.1": True)
    started = []
    monkeypatch.setattr(harnessctl.subprocess, "run",
                        lambda *a, **k: started.append(a) or types.SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    with pytest.raises(harnessctl.HarnessError) as exc:
        harnessctl.start(4096)
    assert "config" in str(exc.value)
    assert not any("enable" in str(a) for a in started), "the unit must not be started"


def test_status_reports_the_seed_the_running_host_carries(monkeypatch):
    """The operator's question is 'is the agent I launch in a project configured?' The
    page answers it from the seed on disk, and says `seeded: false` when there is none."""
    _fresh_state()
    monkeypatch.setattr(harnessctl, "unit_state",
                        lambda unit="ai-harness": {"unit": unit, "active": "active",
                                                   "sub": "running", "main_pid": 5,
                                                   "enabled": True})
    monkeypatch.setattr(harnessctl, "listener", lambda port: {"pid": 5, "line": "x"})
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, host="127.0.0.1", password=None, timeout=6.0:
                        {"ok": True, "auth": "yes", "url": "x"})
    monkeypatch.setattr(harnessctl, "seed_state",
                        lambda port: {"seeded": True, "files": ["opencode.json", "AGENTS.md"],
                                      "cache": "/x/host-4096"})
    st = harnessctl.status(4096)
    assert st["seed"]["seeded"] is True
    assert "AGENTS.md" in st["seed"]["files"]


def test_status_says_the_host_is_unseeded_rather_than_omitting_the_fact(monkeypatch):
    _fresh_state()
    monkeypatch.setattr(harnessctl, "unit_state",
                        lambda unit="ai-harness": {"unit": unit, "active": "active",
                                                   "sub": "running", "main_pid": 5,
                                                   "enabled": True})
    monkeypatch.setattr(harnessctl, "listener", lambda port: {"pid": 5, "line": "x"})
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, host="127.0.0.1", password=None, timeout=6.0:
                        {"ok": True, "auth": "yes", "url": "x"})
    monkeypatch.setattr(harnessctl, "seed_state",
                        lambda port: {"seeded": False, "files": [], "cache": "/x/host-4096"})
    st = harnessctl.status(4096)
    assert st["seed"]["seeded"] is False


def test_the_seed_says_which_files_it_carried(monkeypatch):
    """The page shows a file list, so the seed has to report the files it carried. A seed that
    reported only `ok` would let the page name files the host never received."""
    _fresh_state()
    home = _cache(4097)
    if home.exists():
        shutil.rmtree(home)
    oc = paths.HOME / ".config" / "opencode"
    oc.mkdir(parents=True, exist_ok=True)
    (oc / "opencode.json").write_text(json.dumps({"model": "x"}))
    (oc / "AGENTS.md").write_text("# posture\n")
    out = harnessctl.seed_cache_home(4097)
    assert out["ok"] is True
    assert set(out["files"]) == {"opencode.json", "AGENTS.md"}, out


# --- the launch record: a session carries the project and the personality it was launched with
#
# Queue 33's brief states the personality↔project link in both directions. The projects page can
# answer "is this project served, with what personality" (§6.14.1n) only because the **launch
# site** records the pair — the portal's launch of a project is where the session id, the folder,
# and the operator's personality choice all exist at once. Everywhere else, one of the three is
# already gone by the time anyone asks. A harness start that records nothing leaves the projects
# page answering from the browser's memory of what it launched.

def test_starting_the_host_reports_the_facts_a_launch_needs_to_record(monkeypatch):
    """`start()` is the portal's own call site for a harness launch. Its answer has to carry
    everything a caller needs to record the session, because the caller is the only place that
    knows the project and the personality.

    Asserted as a **shape**, not as a call list: an implementation that recorded the pair in a
    row, in a session field, or in a log satisfies the requirement, and a test naming one
    spelling would block the honest one.
    """
    # A host we are not running: every probe below is answered by the fakes, so the assertions
    # are about the answer's shape rather than about this machine.
    monkeypatch.setattr(harnessctl, "unit_state", lambda: {"ActiveState": "inactive"})
    monkeypatch.setattr(harnessctl, "listener", lambda port: None)
    out = harnessctl.status(port=4099)
    assert out, "status is what start() returns, and it is the answer the launch has to record from"
    assert {"port", "unit"} <= set(out), out
    assert "seed" in out, "the seeded state is shown on the harness page (§6.14.1o)"
    assert "seed" in out["seed"] or out["seed"].get("seeded") is False, out["seed"]


def _host_up(monkeypatch):
    """The host facts a launch is allowed to depend on: the unit owns the port, and the host
    answers health. Patched, not started — no unit is touched by these tests."""
    monkeypatch.setattr(harnessctl, "listener", lambda port: {"pid": "4242"})
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, **k: {"ok": True, "version": "1.19.0", "auth": "yes",
                                           "url": f"http://127.0.0.1:{port}/global/health"})


def _registry(tmp_path, monkeypatch, projects, db):
    """A projects root and a registry in a temporary home, for the tests that need a registry row.

    The root is redirected by *storing the setting*, the way the operator does it from the page
    (tests/test_projects.py's fixture), so the real validator stays in the path.
    """
    home = tmp_path / "home"
    (home / "ai").mkdir(parents=True)
    root = home / "ai-projects"
    root.mkdir()
    from app.core import portalsettings
    monkeypatch.setattr(portalsettings, "home", lambda: str(home))
    monkeypatch.setattr(portalsettings, "portal_home", lambda: str(home / "ai"))
    db.init()
    db.execute("DELETE FROM projects")
    db.set_setting("projects_root", str(root))
    return root


# --- the launch handoff records the pair (queue 33's connection, second half) ----------------
#
# The projects page can answer "is this project served, with what personality" (§6.14.1n) only
# because the launch site records the pair. That call site is where the session id the host
# returned, the project folder, and the operator's personality choice all exist at once; everywhere
# else one of the three is already gone by the time anyone asks. `create_session` is the portal's
# launch, and it is the place the record belongs.

def test_the_launch_launches_inside_the_project_it_is_recorded_as_serving(monkeypatch):
    """The directory the session is created with *is* the project. A launch that created a session
    somewhere else would record a serving fact that the host cannot confirm — the projects page
    would then show a project as served, and `serve_facts` would find no session for its folder."""
    seen = {}
    monkeypatch.setattr(harnessctl, "_request",
                        lambda method, path, body=None, port=4096, query=None, timeout=20:
                        seen.update(method=method, query=query) or (200, {"id": "ses_9"}))
    out = harnessctl.create_session(PROJ)
    assert out["id"] == "ses_9"
    assert seen["query"] == {"directory": PROJ}


def test_launching_a_project_records_the_session_and_the_personality_it_runs_with(tmp_path, monkeypatch):
    """`launch_project` is the portal's launch of a project. It creates the session inside the
    project folder and records the pair on the registry row, in one verb.

    Asserted as a **shape**, not as a call list: an implementation that recorded the pair in a row,
    in a session field, or in a log satisfies the requirement, and a test that named one spelling
    would block the honest one.
    """
    from app.core import db, projects
    sandbox = _registry(tmp_path, monkeypatch, projects, db)

    projects.create("recorded", ["git"], root=str(sandbox))
    _host_up(monkeypatch)
    monkeypatch.setattr(harnessctl, "_request",
                        lambda method, path, body=None, port=4096, query=None, timeout=20:
                        (200, {"id": "ses_55", "directory": str(sandbox / "recorded")}))
    out = harnessctl.launch_project("recorded", personality="Agent-Swift", port=4099)
    assert out["session"] == "ses_55", out
    assert out["personality"] == "Agent-Swift", out
    assert out["path"] == str(sandbox / "recorded"), out
    assert out["recorded"] is True, out

    # The host answers with the session, in the project's folder — which is the fact the page's
    # "is this served" line reads, and the reason the session had to be created there.
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda: {"ok": True, "byDirectory": {str(sandbox / "recorded"):
                                                             [{"id": "ses_55"}]}, "count": 1})
    facts = projects.serve_facts()
    row = [r for r in facts["projects"] if r["name"] == "recorded"][0]
    assert row["serve"]["served"] is True, row["serve"]
    assert row["serve"]["personality"] == "Agent-Swift", row["serve"]


def test_a_launch_that_cannot_record_the_pair_says_so_rather_than_launching_silently(tmp_path,
                                                                                        monkeypatch):
    """The project is not in the registry, so there is no row to record the pair on. The session is
    created — the operator asked for it — and the answer names the missing fact, because a page that
    launched a session it cannot list has to say why the projects page will not show it."""
    from app.core import db, projects
    sandbox = _registry(tmp_path, monkeypatch, projects, db)
    _host_up(monkeypatch)

    monkeypatch.setattr(harnessctl, "_request",
                        lambda method, path, body=None, port=4096, query=None, timeout=20:
                        (200, {"id": "ses_56", "directory": str(sandbox / "loose")}))
    out = harnessctl.launch_project(str(sandbox / "loose"), personality="Agent-Swift", port=4099)
    assert out["session"] == "ses_56", out
    assert out["recorded"] is False, "the pair cannot be recorded on a row that does not exist"
    assert out["reason"], "the answer has to name the fact that is missing"

def test_a_launch_refuses_a_host_that_is_not_answering_before_it_creates_a_session(tmp_path,
                                                                                    monkeypatch):
    """The session is the pair. A session opened on a host that is starting, restarting, or shadowed
    by the first listener on the port is recorded as serving a project and cannot be listed
    afterwards, so the projects page shows a served project the host has no session for.

    The host facts are the same ones the projects payload answers as `launch.available`
    (`projects.launch_facts` reads `status()`), so the operator's button and this verb agree about
    the host, and a launch that got past a disabled-by-the-payload button is refused by the verb
    that performs it.
    """
    from app.core import db, projects
    sandbox = _registry(tmp_path, monkeypatch, projects, db)
    projects.create("quiet", ["git"], root=str(sandbox))

    monkeypatch.setattr(harnessctl, "listener", lambda port: None)
    monkeypatch.setattr(harnessctl, "health",
                        lambda port, **k: {"ok": False, "auth": "none", "note": "URLError"})
    created = []
    monkeypatch.setattr(harnessctl, "_request",
                        lambda method, path, body=None, port=4096, query=None, timeout=20:
                        created.append((method, path)) or (200, {"id": "ses_99"}))

    try:
        harnessctl.launch_project("quiet", personality="default", port=4099)
    except harnessctl.HarnessError as exc:
        assert "not answering" in str(exc), exc
    else:
        raise AssertionError("a session was launched on a host that is not answering")
    assert created == [], "the session was created before the host was checked"
