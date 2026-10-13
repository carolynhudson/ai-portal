"""The harness host: opencode as a portal-managed service, and the portal's client for it.

Canonical queue 30–32 (`docs/PORTAL_PLAN.md` §0.1). The operator's requirement, in their words: a
session a family member starts from the browser keeps running when they change pages, close the
browser, or reboot *their* machine; it survives a portal restart; and after the portal system itself
reboots, the Projects page still names the session id so they can resume it. That is only possible if
the host is **not** a child of the portal. So `opencode serve` runs as its own systemd user unit
(`ai-harness`), and the portal is a client over its HTTP API.

Two measurements shape this module.

**A busy port is invisible.** Measured on this box: a second `opencode serve --port 4096` starts
"successfully" and every request is answered by the **first** server, which holds the socket. A host
that silently shadows is worse than one that fails — the page talks to a stale server with a stale
model list. So the unit's `ExecStartPre` refuses to start on a busy port, and `status()` names the
process that owns the port, so the dashboard can say who it is talking to.

**Sessions live in the server, not the browser.** `~/.local/share/opencode/opencode.db` holds them;
`POST /session` returns an id, and `GET /session?directory=<cwd>` lists a project's sessions. The
directory is a **query parameter** (`?directory=`); a body key with the same name is ignored —
measured: `POST /session {"directory": ...}` created a session in the server's own cwd.

The secret: `OPENCODE_SERVER_PASSWORD` is generated once into `~/ai/state/portal/harness.key`
(0600) and reaches the unit through the env file. The portal reads the value only to sign requests;
its own API exposes presence, never the value (same discipline as `agentconfig.py` reading
`auth.json`).
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import paths, units

HARNESS_UNIT = "ai-harness"
HARNESS_SERVICE = "harness"          # env file / wrapper name: harness.env, ai-harness.sh
DEFAULT_PORT = 4096
DEFAULT_HOST = "127.0.0.1"
HEALTH_PATH = "/global/health"       # measured: {"healthy": true, "version": "1.19.0"}
KEY_FILE_NAME = "harness.key"
_TIMEOUT = 20


class HarnessError(Exception):
    """A refusal the caller can show the operator verbatim."""


def key_path() -> Path:
    return paths.STATE / "portal" / KEY_FILE_NAME


def read_key() -> str | None:
    """The server password, or None. Presence-only reads use `key_present()`."""
    try:
        v = key_path().read_text().strip()
    except OSError:
        return None
    return v or None


def key_present() -> bool:
    return bool(read_key())


def ensure_key(log=print) -> str:
    """Generate the server password once. Never overwrites: a new password invalidates
    every session page the operator has open, and the running server would answer with
    401s the portal cannot explain to them."""
    existing = read_key()
    if existing:
        return existing
    p = key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    p.write_text(value + "\n")
    p.chmod(0o600)
    log(f"generated harness server password at {p}")
    return value


def port_free(port: int, host: str = DEFAULT_HOST) -> bool:
    port = _int(port, DEFAULT_PORT)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def listener(port: int) -> dict[str, Any] | None:
    port = _int(port, DEFAULT_PORT)
    """Who owns the port, from `ss -lptn`. `None` means nothing is listening.

    This is the answer to the shadowing artifact: if a process owns the port and it
    is not the unit's process, the portal is talking to something the unit did not start.
    """
    try:
        out = subprocess.run(
            ["ss", "-lptn", f"sport = :{port}"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines()[1:]:
        if "users:(" not in line:
            continue
        pid = _first_pid(line)
        if pid:
            return {"pid": pid, "line": line.strip()}
    return None


def _first_pid(line: str) -> int | None:
    m = re.search(r"pid=(\d+)", line)
    return int(m.group(1)) if m else None


# The repo root, derived from this file: app/core/harnessctl.py -> repo.
REPO_ROOT = Path(__file__).resolve().parents[2]


def env_mapping(port: int, password: str) -> dict[str, str]:
    return {
        "HARNESS_HOST": DEFAULT_HOST,
        "HARNESS_PORT": str(port),
        "OPENCODE_SERVER_PASSWORD": password,
        # opencode reads its model/provider config from here; the portal's generator
        # (agentconfig.py) writes that file from the router census.
        "OPENCODE_CONFIG_DIR": str(paths.HOME / ".config" / "opencode"),
        # The port pre-flight is a portal module, and systemd starts us from $HOME.
        "HARNESS_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO_ROOT),
    }


def wrapper_body() -> str:
    """The shell the unit execs. Refuses a busy port — the shadowing statement, in the journal."""
    return """#!/bin/sh
# AI portal - harness host wrapper (GENERATED, do not hand-edit).
# opencode serve, as a portal-managed service that is NOT a child of the portal:
# sessions must outlive `systemctl --user restart ai-portal`.
set -eu

BIN="${HARNESS_BIN:-$HOME/.npm-global/bin/opencode}"
if [ ! -x "$BIN" ]; then
  echo "ai-harness: no opencode at $BIN (npm install -g opencode-ai)" >&2
  exit 1
fi
if [ -z "${OPENCODE_SERVER_PASSWORD:-}" ]; then
  echo "ai-harness: OPENCODE_SERVER_PASSWORD is unset; the portal writes it to ~/ai/config/harness.env" >&2
  exit 1
fi

# A second server on a busy port is answered by the first one. Say so and refuse.
if "${HARNESS_PYTHON:-python3}" -m app.core.harnessctl preflight; then :; else
  echo "ai-harness: port ${HARNESS_PORT} is already in use - refusing to start (a shadowed host answers with the first server's state)" >&2
  exit 1
fi

cd "$HOME"
exec "$BIN" serve --port "${HARNESS_PORT}" --hostname "${HARNESS_HOST:-127.0.0.1}"
"""


def unit_text() -> str:
    return f"""[Unit]
Description=AI Portal harness host (opencode serve)
After=network.target

[Service]
EnvironmentFile={paths.CONFIG / 'ai-env.env'}
EnvironmentFile={units.env_path(HARNESS_SERVICE)}
ExecStart={paths.AI_BIN / 'ai-harness.sh'}
WorkingDirectory={paths.HOME}
Restart=on-failure
RestartSec=5
TimeoutStartSec=120

[Install]
WantedBy=default.target
"""


def _int(value: Any, default: int = 0) -> int:
    """systemd and env files give us strings that may be empty or junk."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def base(port: int, host: str = DEFAULT_HOST) -> str:
    return f"http://{host}:{port}"


def _local_request(url: str, data: bytes | None = None, method: str = "GET") -> urllib.request.Request:
    """A Request whose scheme and host are checked, not inherited from a caller.

    Every URL here is built by `base()`, so it is `http://127.0.0.1:<port>`; this is the
    guard that keeps a mis-set host setting from turning the client into a file:// or a
    remote request, which is what the bandit audit on `urlopen` is about.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise HarnessError(f"harness URL must be http, got {parsed.scheme!r}: {url}")
    if parsed.hostname not in ("127.0.0.1", "localhost"):  # path-traversal-scope: loopback only
        raise HarnessError(f"harness host must be loopback, got {parsed.hostname!r}")
    return urllib.request.Request(url, data=data, method=method)


def health(port: int, host: str = DEFAULT_HOST, password: str | None = None,
           timeout: float = 6.0) -> dict[str, Any]:
    """`GET /global/health`. Auth is Basic with the generated password; without it the
    server answers 401, which is reported as `auth_failed`, not as down."""
    url = base(port, host) + HEALTH_PATH
    req = _local_request(url)                      # scheme-pinned: http on 127.0.0.1
    if password:
        tok = base64.b64encode(f"opencode:{password}".encode()).decode()
        req.add_header("authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - local host, pinned scheme
            body = json.loads(r.read().decode() or "{}")
        return {"ok": bool(body.get("healthy")), "version": body.get("version"),
                "auth": "yes", "url": url}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": True, "auth": "auth_failed", "url": url,
                    "note": "the host is up; the portal has no shared password"}
        return {"ok": False, "auth": "yes", "url": url, "note": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"ok": False, "auth": "none", "url": url, "note": f"{type(e).__name__}"}


def unit_state(unit: str = HARNESS_UNIT) -> dict[str, Any]:
    """systemd facts about the host: active state, MainPID, the unit's own sentence."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "UnitFileState"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return {"unit": unit, "error": f"{type(e).__name__}"}
    kv = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v
    return {
        "unit": unit,
        "active": kv.get("ActiveState", "unknown"),
        "sub": kv.get("SubState", ""),
        "main_pid": _int(kv.get("MainPID"), 0),
        "enabled": kv.get("UnitFileState", "") in ("enabled", "alias"),
    }


def status(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> dict[str, Any]:
    """What the harness page needs, and the sentences it must be allowed to say.

    `shadowed` is the point: the unit is not the listener, so whatever answers the port
    was started by someone else and the page would be talking to it as if it were ours.
    """
    u = unit_state()
    port = _int(port, DEFAULT_PORT)
    owner = listener(port)
    pw = read_key()
    h = health(port, host, pw) if owner or u.get("main_pid") else {"ok": False, "auth": "none"}
    main_pid = _int(u.get("main_pid"), 0)
    shadowed = bool(owner and main_pid and owner["pid"] != main_pid)
    out: dict[str, Any] = {
        "unit": u, "port": port, "listening": bool(owner),
        "listener_pid": (owner or {}).get("pid"), "health": h,
        "key_present": key_present(), "shadowed": shadowed,
    }
    owner_pid = (owner or {}).get("pid")
    if shadowed:
        out["note"] = (
            f"port {port} is held by pid {owner_pid}, not by {HARNESS_UNIT} "
            f"(MainPID {main_pid}); a request to this port is answered by that process"
        )
    elif owner and not h["ok"]:
        out["note"] = (
            f"port {port} is held by pid {owner_pid} and did not answer the health probe"
        )
    elif h["ok"] and h.get("auth") == "auth_failed":
        out["note"] = "the host answers, but the portal has no shared password — start it from the portal so the key is written"
    return out


def start(port: int = DEFAULT_PORT, log=print) -> dict[str, Any]:
    """Install env + wrapper + unit, then start the host. Refuses a busy port."""
    port = _int(port, DEFAULT_PORT)
    if not port_free(port):
        owner = listener(port)
        raise HarnessError(
            f"port {port} is in use"
            + (f" by pid {owner['pid']}" if owner else "")
            + "; the harness host refuses to shadow it"
        )
    units.write_env(HARNESS_SERVICE, env_mapping(port, ensure_key(log)), log=log)
    units.write_wrapper(HARNESS_SERVICE, wrapper_body(), log=log)
    upath = units.unit_path(HARNESS_UNIT)
    upath.parent.mkdir(parents=True, exist_ok=True)
    upath.write_text(unit_text())
    log(f"wrote {upath}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=60)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", HARNESS_UNIT],
                       capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise HarnessError(f"{HARNESS_UNIT} did not start: {(r.stderr or r.stdout).strip()[:240]}")
    return status(port)


# --- the client (queue 31: the three flows, measured against the live host) ----------

def _request(method: str, path: str, body: dict | None = None, port: int = DEFAULT_PORT,
            query: dict | None = None, timeout: float = _TIMEOUT) -> tuple[int, Any]:
    pw = read_key()
    if not pw:
        raise HarnessError("no harness password at " + str(key_path())
                           + "; start the harness host from the portal")
    url = base(port) + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = _local_request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    tok = base64.b64encode(f"opencode:{pw}".encode()).decode()
    req.add_header("authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - local host, pinned scheme
            raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        return e.code, raw[:400]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HarnessError(
            f"the harness host at {base(port)} did not answer ({type(e).__name__}: {e}). "
            "It is a separate service: check `systemctl --user status "
            + HARNESS_UNIT + "`"
        ) from e


def list_sessions(cwd: str | None = None, port: int = DEFAULT_PORT) -> list[dict]:
    code, body = _request("GET", "/session", port=port,
                          query={"directory": cwd} if cwd else None)
    if code != 200 or not isinstance(body, list):
        raise HarnessError(f"session list refused (HTTP {code}): {str(body)[:160]}")
    return body


def create_session(cwd: str, port: int = DEFAULT_PORT) -> dict:
    """The directory is a query parameter. Measured: a body key named `directory` is ignored
    and the session lands in the server's own working directory."""
    code, body = _request("POST", "/session", {}, port=port, query={"directory": cwd})
    if code != 200 or not isinstance(body, dict) or "id" not in body:
        raise HarnessError(f"session create refused (HTTP {code}): {str(body)[:160]}")
    return body


def session(session_id: str, cwd: str | None = None, port: int = DEFAULT_PORT) -> dict:
    code, body = _request("GET", f"/session/{session_id}", port=port,
                          query={"directory": cwd} if cwd else None)
    if code != 200 or not isinstance(body, dict):
        raise HarnessError(f"session {session_id} not readable (HTTP {code}): {str(body)[:160]}")
    return body


def prompt(session_id: str, text: str, cwd: str, port: int = DEFAULT_PORT) -> dict:
    code, body = _request("POST", f"/session/{session_id}/message",
                          {"parts": [{"type": "text", "text": text}]},
                          port=port, query={"directory": cwd}, timeout=180)
    if code != 200:
        raise HarnessError(f"prompt refused (HTTP {code}): {str(body)[:200]}")
    return body if isinstance(body, dict) else {"raw": body}


def messages(session_id: str, cwd: str, port: int = DEFAULT_PORT) -> list[dict]:
    code, body = _request("GET", f"/session/{session_id}/message", port=port,
                          query={"directory": cwd})
    if code != 200:
        raise HarnessError(f"messages refused (HTTP {code}): {str(body)[:160]}")
    return body if isinstance(body, list) else []


def preflight(port: int | None = None) -> int:
    """The unit's ExecStartPre exit code. 0 = we may bind, 1 = refuse."""
    p = _int(port or os.environ.get("HARNESS_PORT"), DEFAULT_PORT)
    if port_free(p):
        return 0
    owner = listener(p)
    owner_pid = (owner or {}).get("pid")
    print(f"port {p} in use" + (f" by pid {owner_pid}" if owner_pid else ""), file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    port = _int(os.environ.get("HARNESS_PORT"), DEFAULT_PORT)
    if cmd == "preflight":
        return preflight(port)
    if cmd == "status":
        print(json.dumps(status(port), indent=1))
        return 0
    print("usage: harnessctl [preflight|status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
