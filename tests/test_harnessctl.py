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

import os
import socket
import tempfile
from pathlib import Path

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
