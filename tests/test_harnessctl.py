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

def test_create_session_binds_the_location_in_the_body(monkeypatch):
    """Measured on 2.0.29: V2 dropped the V1 `?directory=` query — `location.directory` in
    the body is the shape the create accepts, and the echoed `Session.Info` names it back."""
    seen = {}

    def fake_request(method, path, body=None, port=4096, query=None, timeout=20):
        seen["method"], seen["path"], seen["body"], seen["query"] = method, path, body, query
        return 200, {"data": {"id": "ses_1", "location": {"directory": "/home/x/proj"}}}
    monkeypatch.setattr(harnessctl, "_request", fake_request)
    out = harnessctl.create_session("/home/x/proj")
    assert out["id"] == "ses_1"
    assert seen["method"] == "POST" and seen["path"] == "/api/session"
    assert seen["body"] == {"location": {"directory": "/home/x/proj"}}
    assert seen["query"] is None, "V2 binds the session's location at creation"


def test_list_sessions_filters_by_cwd_and_refuses_a_non_list(monkeypatch):
    monkeypatch.setattr(harnessctl, "_request",
                        lambda *a, **k: (200, {"data": [{"id": "ses_1"}]}))
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
        return 200, {"data": {"id": "msg_1", "type": "user", "delivery": "steer"}}
    monkeypatch.setattr(harnessctl, "_request", fake_request)
    out = harnessctl.prompt("ses_1", "hello", PROJ)
    assert out["queued"] is True, "the inbox receipt is a queue receipt, not a finished turn"
    assert seen["path"] == "/api/session/ses_1/prompt", \
        "the V1 synchronous /message verb blocks the whole turn and trips the client's read timeout"
    assert seen["body"] == {"text": "hello"}
    assert seen["query"] is None, "the session's location is bound at creation (measured on 2.0.29)"


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


def test_the_seed_writes_the_harness_plugin_list_the_host_can_load(monkeypatch):
    """The harness is a V2 host (measured 2.0.29): it loads `plugins` entries with
    `Plugin.define` packages, and its compatibility layer normalizes the V1 `plugin` key —
    so a copy carrying both keys hands V1-exporting packages to a loader that refuses them.
    The seed writes `plugins` = HARNESS_V2_PLUGINS, pinned, and drops the V1 key."""
    _fresh_state()
    home = _cache(4098)
    if home.exists():
        shutil.rmtree(home)
    oc = paths.HOME / ".config" / "opencode"
    oc.mkdir(parents=True, exist_ok=True)
    (oc / "opencode.json").write_text(json.dumps({
        "model": "llamacpp/x",
        "plugin": ["opencode-websearch-cited"],
    }))
    out = harnessctl.seed_cache_home(4098)
    assert out["ok"] is True
    seeded = json.loads((home / "opencode.json").read_text())
    assert seeded["plugins"] == [{"package": p} for p in harnessctl.HARNESS_V2_PLUGINS], \
        seeded.get("plugins")
    assert "plugin" not in seeded, "the V1 key must not travel to a host whose loader normalizes it"
    assert seeded["model"] == "llamacpp/x", "the copy keeps the rest of the generated config"


def test_the_host_is_told_to_read_the_seeded_directory(monkeypatch):
    """Measured: the host reads its whole config from `OPENCODE_CONFIG_DIR` at startup.
    The env must point it at the seeded directory, not at the CLI's global config.
    The data home must be the host's own too (measured ref=err_4c90c294: a shared
    opencode.db migrated by a newer opencode breaks the other version's instance boot),
    and the binary must be the V2 one the plugin ecosystem is moving to."""
    _fresh_state()
    mapping = harnessctl.env_mapping(4096, "probe")
    assert mapping["OPENCODE_CONFIG_DIR"] == str(harnessctl.cache_dir(4096))
    assert mapping["XDG_DATA_HOME"] == str(harnessctl.cache_dir(4096) / "data")
    assert mapping["HARNESS_BIN"].endswith("/.opencode/bin/opencode"), mapping["HARNESS_BIN"]
    assert "XDG_CACHE_HOME" not in mapping, "the unit never carried it; the config dir is the seam"


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
                        lambda port, **k: {"ok": True, "version": "2.0.29", "auth": "yes",
                                           "url": f"http://127.0.0.1:{port}/api/info"})


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
                        seen.update(method=method, body=body) or
                        (200, {"data": {"id": "ses_9"}}))
    out = harnessctl.create_session(PROJ)
    assert out["id"] == "ses_9"
    assert seen["body"] == {"location": {"directory": PROJ}}


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
                        (200, {"data": {"id": "ses_55",
                                        "location": {"directory": str(sandbox / "recorded")}}}))
    out = harnessctl.launch_project("recorded", personality="Agent-Swift", port=4099)
    assert out["session"] == "ses_55", out
    assert out["personality"] == "Agent-Swift", out
    assert out["path"] == str(sandbox / "recorded"), out
    assert out["recorded"] is True, out

    # The host answers with the session, in the project's folder — which is the fact the page's
    # "is this served" line reads, and the reason the session had to be created there.
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": True, "byDirectory": {str(sandbox / "recorded"):
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
                        (200, {"data": {"id": "ses_56",
                                        "location": {"directory": str(sandbox / "loose")}}}))
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


# --- the wire shape, measured against the running host (queue 32, plan §6.14.1n) ----
#
# Two facts were measured on the live 1.19.0 host and neither is visible from the client's
# own tests, because every earlier test replaced `_request` and never looked at what
# `_request` puts on the wire:
#
# 1. A body key whose value is a list must arrive as a **JSON array**. Sending
#    `parts` as a JSON *string* is answered `400 ... Expected string, got
#    [{"type":"text",...}] at ["parts"][0]["text"]` — the prompt verb, which is the verb
#    the session view's send button posts to, simply fails.
# 2. `GET /session` with no `directory` answers the **server's own project scope** — the
#    project folders the projects page launches into are not in that list at all. The
#    live-session read has to ask per folder, or `serve_facts` answers "not on the host"
#    about a session it created a minute earlier.

def test_the_prompt_body_reaches_the_wire_as_a_real_json_array(monkeypatch):
    """The host takes `parts` as a JSON **array**. Measured on 1.19.0: send it as a JSON *string*
    and the answer is `400 ... Expected string, got [{"type":"text",...}] at
    ["parts"][0]["text"]` — the verb the session view's send button posts is simply refused.

    Every other test in this file replaces `_request`, so none of them ever looked at the bytes
    `_request` puts on the wire. This one pins the encoding at the socket boundary.
    """
    _fresh_state()
    harnessctl.ensure_key(log=lambda *a: None)
    captured = {}

    class _Reply:
        status = 204

        def read(self):
            return b''

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _Reply()
    monkeypatch.setattr(harnessctl.urllib.request, "urlopen", fake_urlopen)
    harnessctl.prompt("ses_1", "hello", PROJ)
    body = json.loads(captured["data"].decode())
    assert body == {"text": "hello"}, \
        f"the wire body was {captured['data']!r}"
    assert "/api/session/ses_1/prompt" in captured["url"], \
        f"the send went to {captured['url']!r}"
    assert "?directory=" not in captured["url"], \
        "V2 binds the session's location at creation; the send carries no per-request directory"


def test_the_live_session_read_asks_the_host_for_each_folder_it_cares_about(monkeypatch):
    """`GET /api/session` unscoped answers the **server's own location** (measured on
    2.0.29): it lists the sessions of the folder the host was started in, not the projects.
    The aggregate has to be built by asking for each folder, or a project's own session is
    invisible to the projects page."""
    asked = []

    def fake(method, path, body=None, port=4096, query=None, timeout=20):
        asked.append((method, path, query))
        directory = (query or {}).get("directory")
        return 200, {"data": [{"id": "ses_x", "location": {"directory": directory}}]}
    monkeypatch.setattr(harnessctl, "_request", fake)
    out = harnessctl.live_sessions(["/a/proj", "/b/proj"])
    assert [q.get("directory") for _m, _p, q in asked] == ["/a/proj", "/b/proj"]
    assert [s["location"]["directory"] for s in out] == ["/a/proj", "/b/proj"]


def test_a_folder_that_the_host_refuses_to_list_does_not_erase_the_other_folders(monkeypatch):
    def fake(method, path, body=None, port=4096, query=None, timeout=20):
        directory = (query or {}).get("directory")
        if directory == "/bad":
            return 500, "boom"
        return 200, {"data": [{"id": "ses_ok", "location": {"directory": directory}}]}
    monkeypatch.setattr(harnessctl, "_request", fake)
    out = harnessctl.live_sessions(["/bad", "/good"])
    assert [s["id"] for s in out] == ["ses_ok"]


# --- the session view's reads (queue 32: the operational state) ---------------------

SESSION = {
    "id": "ses_view", "title": "Agentic run", "location": {"directory": PROJ},
    "agent": "build", "model": {"providerID": "llamacpp", "id": "m1"},
    "cost": 0.0, "tokens": {"input": 9255, "output": 51, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}},
    "time": {"created": 1792042416456, "updated": 1792043019155},
}
TRANSCRIPT = [
    {"type": "user", "id": "msg_u", "text": "do the thing", "time": {"created": 1}},
    {"type": "assistant", "id": "msg_a", "agent": "build",
     "model": {"providerID": "llamacpp", "id": "m1"}, "cost": 0, "finish": "stop",
     "time": {"created": 2, "completed": 3},
     "tokens": {"input": 27, "output": 28, "reasoning": 0, "cache": {"read": 9250, "write": 0}},
     "content": [{"type": "tool", "name": "bash", "state": {"status": "completed"}},
                 {"type": "text", "text": "done"}]},
    {"type": "idle", "id": "msg_i", "outcome": "succeeded", "time": {"created": 4}},
]


def _script_host(monkeypatch, routes):
    """Route the client through a scripted host: `routes` maps (method, path) to (code, body).

    Paths are matched without the query string, which every one of these verbs carries.
    """
    seen = []

    def fake(method, path, body=None, port=4096, query=None, timeout=20):
        seen.append({"method": method, "path": path, "body": body, "query": query})
        for (m, p), reply in routes.items():
            if m == method and path.split("?")[0] == p:
                return reply
        return 404, {"error": f"unscripted {method} {path}"}
    monkeypatch.setattr(harnessctl, "_request", fake)
    return seen


def test_the_session_view_reports_the_facts_the_session_state_shows(monkeypatch):
    seen = _script_host(monkeypatch, {
        ("GET", "/api/session/ses_view"): (200, {"data": SESSION}),
        ("GET", "/api/session/ses_view/message"): (200, {"data": TRANSCRIPT}),
        ("GET", "/api/session/active"): (200, {"data": {"ses_view": {"type": "running"}}}),
        ("GET", "/api/form"): (200, {"location": {"directory": PROJ}, "data": []}),
    })
    view = harnessctl.session_view("ses_view", PROJ, context_limit=131072)
    assert view["ok"] and view["found"], view
    assert view["session"] == "ses_view"
    assert view["busy"] is True, "a session the host reports running must not be shown as idle"
    assert view["model"] == {"providerID": "llamacpp", "modelID": "m1"}, \
        "the model badge is read from the session's own model ref, not from a setting"
    assert view["context_used"] == 9305, "the meter counts the step the host reported"
    assert view["context_limit"] == 131072
    assert [r["role"] for r in view["transcript"]] == ["user", "assistant"], \
        "idle rows close a turn; they are bookkeeping, not bubbles"
    assert view["transcript"][1]["text"] == "done"
    assert view["transcript"][1]["tools"] == [{"tool": "bash", "state": "completed"}]
    # V2 binds the session's location at creation: the prompt, message, active, and form
    # reads carry no per-request directory — and none of them may start carrying one.
    for s in seen:
        assert not (s["query"] or {}).get("directory")


def test_the_session_view_says_the_host_has_no_session_instead_of_calling_it_idle(monkeypatch):
    _script_host(monkeypatch, {
        ("GET", "/api/session/ses_gone"): (404, {"name": "SessionNotFoundError"}),
        ("GET", "/api/session/ses_gone/message"): (404, {"name": "SessionNotFoundError"}),
        ("GET", "/api/session/active"): (200, {"data": {}}),
        ("GET", "/api/form"): (200, {"location": {"directory": PROJ}, "data": []}),
    })
    view = harnessctl.session_view("ses_gone", PROJ)
    assert view["found"] is False, "a session the host does not know is reported as gone, never idle"
    assert view["busy"] is None
    assert view["reason"], view


def test_the_session_view_names_a_host_that_is_not_answering(monkeypatch):
    def boom(method, path, body=None, port=4096, query=None, timeout=20):
        raise harnessctl.HarnessError("the harness host at http://127.0.0.1:4096 did not answer")
    monkeypatch.setattr(harnessctl, "_request", boom)
    view = harnessctl.session_view("ses_view", PROJ)
    assert view["ok"] is False and view["found"] is None
    assert "did not answer" in view["error"]


def test_a_pending_question_is_carried_into_the_view_as_a_form(monkeypatch):
    _script_host(monkeypatch, {
        ("GET", "/api/session/ses_view"): (200, {"data": SESSION}),
        ("GET", "/api/session/ses_view/message"): (200, {"data": TRANSCRIPT}),
        ("GET", "/api/session/active"): (200, {"data": {"ses_view": {"type": "running"}}}),
        ("GET", "/api/form"): (200, {"location": {"directory": PROJ}, "data": [{
            "id": "frm_1", "sessionID": "ses_view", "title": "Which branch?",
            "fields": [{"key": "branch", "title": "Which branch?", "type": "string",
                        "options": [{"value": "main", "label": "main"},
                                     {"value": "dev", "label": "dev"}]}]}]}),
    })
    view = harnessctl.session_view("ses_view", PROJ)
    q = view["question"]
    assert q and q["id"] == "frm_1"
    assert q["questions"][0]["key"] == "branch", \
        "the answer travels keyed by the field key — the page needs it on the input"
    assert q["questions"][0]["options"] == [{"value": "main", "label": "main"},
                                             {"value": "dev", "label": "dev"}]
    assert q["questions"][0]["multiSelect"] is False


def test_a_question_from_another_session_is_not_shown_as_this_sessions_form(monkeypatch):
    _script_host(monkeypatch, {
        ("GET", "/api/session/ses_view"): (200, {"data": SESSION}),
        ("GET", "/api/session/ses_view/message"): (200, {"data": TRANSCRIPT}),
        ("GET", "/api/session/active"): (200, {"data": {}}),
        ("GET", "/api/form"): (200, {"location": {"directory": PROJ},
                                      "data": [{"id": "frm_other", "sessionID": "ses_other",
                                                "title": "x",
                                                "fields": [{"key": "k", "title": "x",
                                                            "type": "string"}]}]}),
    })
    assert harnessctl.session_view("ses_view", PROJ)["question"] is None


# --- the session view's verbs ------------------------------------------------------

def test_the_session_view_sends_the_prompt_as_the_host_measures_it(monkeypatch):
    seen = _script_host(monkeypatch, {
        # Measured on 2.0.29: the prompt verb answers 200 with the inbox entry it created.
        ("POST", "/api/session/ses_view/prompt"): (200, {"data": {"id": "msg_9", "type": "user"}}),
    })
    out = harnessctl.send_prompt("ses_view", "fix the test", PROJ)
    assert out["ok"] and out["accepted"] is True and out["queued"] is True
    assert out["message"] == "msg_9", "the inbox receipt names the message the host created"
    assert "reason" in out, "the API route logs out['reason'] on every send, success included"
    call = seen[0]
    assert call["method"] == "POST" and call["path"].startswith("/api/session/ses_view/prompt")
    assert call["body"] == {"text": "fix the test"}, \
        f"the body went out as {call['body']!r}"
    assert call["query"] is None, "the location is bound at creation (measured on 2.0.29)"


def test_the_transcript_fills_a_bubble_from_what_the_host_actually_sends(monkeypatch):
    """Measured on this box: its local models answer with `reasoning` + `tool` parts and
    often no text part at all, and a turn in progress is a row whose content has not been
    written. A row built from text parts alone printed every such answer as an empty
    bubble — the operator's 'series of empty bubbles'."""
    _script_host(monkeypatch, {
        ("GET", "/api/session/ses_b/message"): (200, {"data": [
            {"type": "user", "id": "msg_u", "text": "hi"},
            {"type": "assistant", "id": "msg_r",
             "content": [{"type": "reasoning", "text": "the user said hi; I answer"},
                          {"type": "tool", "name": "bash", "state": {"status": "completed"}}]},
            {"type": "assistant", "id": "msg_t",
             "content": [{"type": "tool", "name": "read", "state": {"status": "completed"}}]},
            {"type": "assistant", "id": "msg_w", "content": []},
        ]}),
    })
    rows = harnessctl.transcript("ses_b", PROJ)
    assert rows[1]["text"] == "the user said hi; I answer", \
        f"reasoning-only answer painted as {rows[1]['text']!r}"
    assert rows[2]["text"] == "read completed", \
        f"tool-only step painted as {rows[2]['text']!r}"
    assert rows[3]["text"] == "…", f"a mid-turn row painted as {rows[3]['text']!r}"


def test_a_refused_prompt_is_the_hosts_refusal_with_the_hosts_reason(monkeypatch):
    _script_host(monkeypatch, {
        ("POST", "/api/session/ses_view/prompt"): (400, {"name": "InvalidRequestError",
                                                          "data": {"message": "no text"}}),
    })
    out = harnessctl.send_prompt("ses_view", "x", PROJ)
    assert out["ok"] is False, "the page's verb answers, it does not raise"
    assert "400" in out["reason"] and "no text" in out["reason"], \
        f"the host's own words travel to the page: {out['reason']!r}"


def test_answering_a_form_sends_the_answer_keyed_by_the_field_keys(monkeypatch):
    seen = _script_host(monkeypatch, {
        ("POST", "/api/session/ses_view/form/frm_1/reply"): (200, {"data": True}),
    })
    out = harnessctl.answer_question("ses_view", "frm_1",
                                     {"branch": "main", "extras": ["a", "b"]})
    assert out["ok"] is True
    assert seen[0]["body"] == {"answer": {"branch": "main", "extras": ["a", "b"]}}, \
        "measured on 2.0.29: Form.Answer is an object keyed by the form's field keys"


def test_refusing_a_question_is_a_verb_of_its_own(monkeypatch):
    seen = _script_host(monkeypatch, {("DELETE", "/api/session/ses_view/form/frm_1"): (204, None)})
    assert harnessctl.reject_question("ses_view", "frm_1")["ok"] is True
    assert seen[0]["method"] == "DELETE" and seen[0]["path"] == "/api/session/ses_view/form/frm_1"


def test_stopping_a_session_interrupts_it_on_the_host(monkeypatch):
    seen = _script_host(monkeypatch, {
        ("POST", "/api/session/ses_view/interrupt"): (200, {"interrupted": True}),
    })
    out = harnessctl.stop_session("ses_view", PROJ)
    assert out["ok"] is True
    assert seen[0]["path"].startswith("/api/session/ses_view/interrupt")
    assert seen[0]["query"] is None, "the session's location is bound; the verb needs no directory"


def test_the_view_states_the_tokens_the_session_has_used(monkeypatch, tmp_path):
    sandbox = tmp_path
    """Queue 32's budget line needs the used half, and the host's `tokens` is a nested shape
    (`{input, output, reasoning, cache:{read,write}}`), measured on the live host. A page that summed
    the dict's values would add the cache sub-object and crash; a page that read `tokens.total` would
    show nothing, because the host has no `total`."""
    def fake(method, path, body=None, port=4096, query=None, **kw):
        if path.endswith("/api/session/ses_t"):
            return (200, {"data": {"id": "ses_t", "location": {"directory": str(sandbox)},
                                   "tokens": {"input": 8814, "output": 15, "reasoning": 0,
                                              "cache": {"read": 0, "write": 0}}}})
        return (200, {"data": []})

    monkeypatch.setattr(harnessctl, "_request", fake)
    out = harnessctl.session_view("ses_t", str(sandbox))
    assert out["context_used"] == 8829, out
    monkeypatch.setattr(harnessctl, "_request",
                        lambda method, path, body=None, port=4096, query=None, **kw:
                        (200, {"data": {"id": "ses_t", "location": {"directory": str(sandbox)}}})
                        if path.endswith("/api/session/ses_t") else (200, {"data": []}))
    assert harnessctl.session_view("ses_t", str(sandbox))["context_used"] is None, \
        "a session with no token figures has no used count — not zero, which reads as an empty context"


def test_a_prompt_that_the_host_refuses_says_what_the_host_said(monkeypatch, tmp_path):
    sandbox = tmp_path
    """State 3 sends the text. The host's refusal is the answer the page has to print — it is the
    difference between 'the box did nothing' and 'this session cannot take text'."""
    def fake(method, path, body=None, port=4096, query=None, **kw):
        return (400, {"error": {"name": "InvalidRequestError",
                                "message": "session is compacting; try again after it finishes"}})

    monkeypatch.setattr(harnessctl, "_request", fake)
    out = harnessctl.send_prompt("ses_x", "continue", str(sandbox))
    assert out["ok"] is False, out
    assert "compacting" in out["reason"], out
    assert out["reason"] != "the host answered 400", \
        f"the host's own words are the useful half: {out['reason']!r}"


def test_an_empty_send_is_refused_by_the_portal_itself(monkeypatch, tmp_path):
    """The portal has no reason to ask the host about text it can see is empty, and a host that
    accepted it would create a message the operator never wrote."""
    def fake(*a, **kw):
        raise AssertionError("the portal asked the host about an empty send")

    monkeypatch.setattr(harnessctl, "_request", fake)
    out = harnessctl.send_prompt("ses_x", "   ", str(tmp_path))
    assert out["ok"] is False and "empty" in out["reason"], out
