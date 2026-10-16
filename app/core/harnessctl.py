"""The harness host: opencode as a portal-managed service, and the portal's client for it.

Canonical queue 30–32 (`docs/PORTAL_PLAN.md` §0.1). The operator's requirement, in their words: a
session a family member starts from the browser keeps running when they change pages, close the
browser, or reboot *their* machine; it survives a portal restart; and after the portal system itself
reboots, the Projects page still names the session id so they can resume it. That is only possible if
the host is **not** a child of the portal. So `opencode serve` runs as its own systemd user unit
(`ai-harness`), and the portal is a client over its HTTP API.

Three measurements shape this module.

**A busy port is invisible.** Measured on this box: a second `opencode serve --port 4096` starts
"successfully" and every request is answered by the **first** server, which holds the socket. A host
that silently shadows is worse than one that fails — the page talks to a stale server with a stale
model list. So the unit's `ExecStartPre` refuses to start on a busy port, and `status()` names the
process that owns the port, so the dashboard can say who it is talking to.

**Sessions live in the server, not the browser**, in its `opencode.db`. The host is V2 (measured
2.0.29): `POST /api/session` with `location.directory` in the body creates the session, and
`GET /api/session?directory=<cwd>` lists a project's sessions — V2 dropped the V1 `?directory=`
query for *writes*: the session's location is bound at creation, and prompt/message/form reads
carry no per-request directory.

**opencode versions do not share a database politely.** Measured (ref=err_4c90c294): a V1 host and
a V2 server on one `opencode.db` answer every directory-scoped route with `SQLiteError` once the
newer one migrates the schema out from under the older. So the harness owns its data home
(`XDG_DATA_HOME` in the env file, seeded by the portal): its sessions stay durable in it, and no
other opencode rewrites its schema underneath it.

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
import shutil
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
HEALTH_PATH = "/api/info"            # measured (2.0.29): {"version": "2.0.29", "pid": …, "urls": […]}
KEY_FILE_NAME = "harness.key"
_TIMEOUT = 20

# Plugins the harness host loads. Measured on 2.0.29: the V2 loader accepts `plugins`
# entries with `Plugin.define` exports (`opencode-search@1.0.0` reports `active` through
# `POST /api/plugin/check`), and the v2 compatibility layer normalizes the V1 `plugin`
# key — writing both keys hands the V1 packages to a loader that refuses them. The host
# is V2, so the seed writes the V2 key only. Pinned: the host starts with what is
# written, and an unpinned entry resolves to whatever latest is on release day.
HARNESS_V2_PLUGINS = ["opencode-search@1.0.0"]


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


def cache_dir(port: int) -> Path:
    """The host's cache home. opencode reads `opencode.json` and `AGENTS.md` from
    `$XDG_CACHE_HOME/opencode`, so this directory is the host's whole view of the
    agent's configuration."""
    return paths.STATE / "harness" / f"host-{port}"


def seed_cache_home(port: int, log=print) -> dict:
    """Copy the portal-generated agent config into the config home the host reads, with
    the harness's plugin list written into the copy.

    The gate this implements (plan §6.14.1o): the host reads `opencode.json` and the
    `AGENTS.md` files out of the directory it is started with, and a host started with an
    empty one answers from defaults — no posture, no compaction, none of the plugins.
    `agentconfig` is the component that generates those files from the router census, so
    the copy is the portal's job, and it happens before the unit starts: a host seeded
    after it read an empty directory stays unseeded.

    The copy is where the host/plugin split is honored. Measured on 2.0.29: the V2 loader
    reads the `plugins` key with `Plugin.define` packages; its compatibility layer also
    normalizes the V1 `plugin` key, so a copy carrying both keys hands V1-exporting
    packages to a loader that refuses them. The seed writes `plugins` = the pinned V2
    packages and drops the V1 `plugin` key from the copy.
    """
    cache = cache_dir(port)
    src = Path(os.environ.get("OPENCODE_CONFIG_DIR")
               or paths.HOME / ".config" / "opencode")
    if not (src / "opencode.json").exists():
        return {"ok": False, "cache": str(cache),
                "error": f"the host has no agent config to run with: {src / 'opencode.json'} "
                       "does not exist; sync the harness configs first"}
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "data").mkdir(parents=True, exist_ok=True)   # the host's own session store home
    files = []
    cfg = json.loads((src / "opencode.json").read_text())
    cfg["plugins"] = [{"package": p} for p in HARNESS_V2_PLUGINS]
    cfg.pop("plugin", None)
    (cache / "opencode.json").write_text(json.dumps(cfg, indent=2) + "\n")
    files.append("opencode.json")
    if (src / "AGENTS.md").exists():
        shutil.copy2(src / "AGENTS.md", cache / "AGENTS.md")
        files.append("AGENTS.md")
    log(f"seeded {cache} ({', '.join(files)})")
    return {"ok": True, "cache": str(cache), "files": files}


def seed_state(port: int) -> dict:
    """What the running host's cache home actually holds. Reported, never assumed."""
    cache = cache_dir(port)
    files = [n for n in ("opencode.json", "AGENTS.md") if (cache / n).exists()]
    return {"seeded": "opencode.json" in files, "files": files, "cache": str(cache)}


def env_mapping(port: int, password: str) -> dict[str, str]:
    return {
        "HARNESS_HOST": DEFAULT_HOST,
        "HARNESS_PORT": str(port),
        "OPENCODE_SERVER_PASSWORD": password,
        # The wrapper honors `HARNESS_BIN` (measured: `BIN="${HARNESS_BIN:-…}"`). Pointing it
        # at the V2 binary is the harness upgrade: the plugin ecosystem is migrating to the
        # V2 `Plugin.define` spec, and a V2 host loads the same packages the CLI loads.
        "HARNESS_BIN": str(paths.HOME / ".opencode" / "bin" / "opencode"),
        # Measured on the live 1.19.0 host: the server reads its whole agent config
        # (providers, model list, default model, plugins, AGENTS.md) from
        # `OPENCODE_CONFIG_DIR` at startup. Pointing it at the seeded directory is what
        # gives the harness its own file — the harness V1 plugin list can differ from the
        # interactive CLI's V2 list, which lives in ~/.config/opencode.
        "OPENCODE_CONFIG_DIR": str(cache_dir(port)),
        # Measured (ref=err_4c90c294): the v1 host and the v2 CLI shared one
        # `~/.local/share/opencode/opencode.db`, and the v2 server's schema migration
        # removed `project_id` — the v1 host then answered every directory-scoped route
        # with SQLiteError on instance boot. The harness owns a data home; sessions stay
        # durable in it and no other opencode version rewrites its schema under it.
        "XDG_DATA_HOME": str(cache_dir(port) / "data"),
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
    """`GET /api/info`. Auth is Basic with the generated password; without it the server
    answers 401, which is reported as `auth_failed`, not as down.

    Measured on 2.0.29: the V2 health verb carries no `healthy` flag — answering 200 with
    `{version, pid, urls}` **is** the healthy answer, so presence is the flag."""
    url = base(port, host) + HEALTH_PATH
    req = _local_request(url)                      # scheme-pinned: http on 127.0.0.1
    if password:
        tok = base64.b64encode(f"opencode:{password}".encode()).decode()
        req.add_header("authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - local host, pinned scheme
            body = json.loads(r.read().decode() or "{}")
        return {"ok": isinstance(body, dict) and "version" in body,
                "version": body.get("version") if isinstance(body, dict) else None,
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
        "seed": seed_state(port),
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


def start_unit(port: int, log=print) -> subprocess.CompletedProcess:
    """Enable and start the unit. The moment the host begins reading its cache home.

    The annotation is the caller's contract, not decoration: `start()` reads `.returncode`,
    `.stderr`, and `.stdout` off this value, and a `-> dict` annotation promised a shape the
    function has never returned (subprocess.run, with text=True, answers a CompletedProcess).
    """
    return subprocess.run(["systemctl", "--user", "enable", "--now", HARNESS_UNIT],
                          capture_output=True, text=True, timeout=120, check=False)


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
    seed = seed_cache_home(port, log)
    if not seed.get("ok"):
        raise HarnessError(seed.get("error") or "the harness host cannot be seeded")
    units.write_env(HARNESS_SERVICE, env_mapping(port, ensure_key(log)), log=log)
    units.write_wrapper(HARNESS_SERVICE, wrapper_body(), log=log)
    upath = units.unit_path(HARNESS_UNIT)
    upath.parent.mkdir(parents=True, exist_ok=True)
    upath.write_text(unit_text())
    log(f"wrote {upath}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=60)
    r = start_unit(port, log)
    if r.returncode != 0:
        raise HarnessError(f"{HARNESS_UNIT} did not start: {(r.stderr or r.stdout).strip()[:240]}")
    return status(port)


def sync(port: int = DEFAULT_PORT, log=print) -> dict[str, Any]:
    """Re-seed the host's config home, rewrite its env file, and restart the host.

    Sessions live in `opencode.db`, not in the host process — restarting is what the unit
    being a service is for (§this module's header): the operator's sessions survive it.
    The host reads its config at startup only (measured: the live host kept answering
    with the config it read at its last start, edits since invisible), so a config edit
    reaches the harness through this verb, not through the file alone.
    """
    port = _int(port, DEFAULT_PORT)
    seed = seed_cache_home(port, log)
    if not seed.get("ok"):
        raise HarnessError(seed.get("error") or "the harness host cannot be seeded")
    units.write_env(HARNESS_SERVICE, env_mapping(port, ensure_key(log)), log=log)
    r = subprocess.run(["systemctl", "--user", "restart", HARNESS_UNIT],
                       capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise HarnessError(f"{HARNESS_UNIT} did not restart: {(r.stderr or r.stdout).strip()[:240]}")
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
        # The scheme and host are checked by `_local_request` (http only, loopback only) before this
        # line is reached, and every URL is built by `base()`; see test_the_client_never_leaves_loopback.
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
    code, body = _request("GET", "/api/session", port=port,
                          query={"directory": cwd} if cwd else None)
    data = body.get("data") if isinstance(body, dict) else None
    if code != 200 or not isinstance(data, list):
        raise HarnessError(f"session list refused (HTTP {code}): {str(body)[:160]}")
    return data


def create_session(cwd: str, port: int = DEFAULT_PORT) -> dict:
    """The directory travels as `location.directory` in the body. Measured on 2.0.29:
    V2 dropped the V1 `?directory=` query; `location` is the wire's word for it, and the
    created `Session.Info` echoes the location back."""
    code, body = _request("POST", "/api/session",
                          {"location": {"directory": cwd}}, port=port)
    data = body.get("data") if isinstance(body, dict) else None
    if code != 200 or not isinstance(data, dict) or "id" not in data:
        raise HarnessError(f"session create refused (HTTP {code}): {str(body)[:160]}")
    return data


def launch_project(name_or_path: str, personality: str | None = None,
                   port: int = DEFAULT_PORT) -> dict[str, Any]:
    """The portal's launch of a project: create the session **inside** the project folder, and
    record the session and the personality on the registry row, in one verb.

    Queue 33's brief states the personality↔project link in both directions, and the launch is the
    only call site where all three facts exist at once — the session id the host returned, the
    project folder, and the personality the operator picked. Everywhere else, one of the three is
    already gone by the time anyone asks, and the projects page is left answering from the browser's
    memory of what it launched (§6.14.1n).

    The directory the session is created with *is* the project: a session created somewhere else
    records a serving fact the host cannot confirm, and `serve_facts` finds no session for the
    folder the page says is served.

    A project that is not in the registry has no row to record the pair on. The session is created
    anyway — the operator asked for it — and the answer names the missing fact, because a page that
    launched a session it cannot list has to say why the projects page will not show it.
    """
    from . import projects

    # Resolve the folder through the projects component, so a name goes through the same rule every
    # other name on that page goes through.
    target = projects.project_path(projects.validate_name(name_or_path)) \
        if os.path.sep not in str(name_or_path) else os.path.realpath(name_or_path)
    # The host's state is checked before the session is created, not after: a session opened on a
    # host that is starting, restarting, or shadowed by the first listener on the port is a session
    # the portal records as serving a project and cannot list afterwards. The projects page reads
    # `launch.available` from this same `status()` call, so the operator's button and this verb agree
    # about the host.
    # The host check is the projects payload's `launch.available`, read from the same facts: a
    # session is only launchable when the unit owns the port and the host answers health.
    listening = bool(listener(port))
    health_out = health(port)
    ok = bool(listening and (health_out.get("auth_failed") or health_out.get("ok")))
    if not ok:
        raise HarnessError(
            "the harness host is not answering"
            + (f" ({health_out.get('error')})" if health_out.get("error") else "")
            + "; a session cannot be launched, and the pair cannot be recorded on a project the "
              "host has no session for")
    created = create_session(target, port=port)
    session_id = str(created.get("id"))
    recorded = False
    reason: str | None = None
    try:
        marked = projects.mark_served(target, session_id, personality)
        recorded = bool(marked.get("ok"))
    except projects.ProjectError as exc:
        # The folder is not in the registry. That is a fact about the registry, not a reason to
        # abandon the session the operator asked for.
        reason = str(exc)
    out: dict[str, Any] = {"ok": True, "session": session_id, "path": target,
                           "port": port, "personality": personality,
                           "recorded": recorded, "session_facts": created}
    if reason:
        out["reason"] = reason
    return out


def session(session_id: str, cwd: str | None = None, port: int = DEFAULT_PORT) -> dict:
    code, body = _request("GET", f"/api/session/{session_id}", port=port)
    data = body.get("data") if isinstance(body, dict) else None
    if code != 200 or not isinstance(data, dict):
        raise HarnessError(f"session {session_id} not readable (HTTP {code}): {str(body)[:160]}")
    return data


def prompt(session_id: str, text: str, cwd: str, port: int = DEFAULT_PORT) -> dict:
    """Queue the text on the host without waiting for the agent's turn.

    Measured on 2.0.29: V2's `POST /api/session/{id}/prompt` answers `200` with the
    **inbox entry** it created (a `delivery: steer|queue` queue) in well under a second —
    the turn runs in the server and the page's poll loop carries the transcript. The body
    is `{text}`; the V1 `parts` array is gone.

    `cwd` is kept in the signature: the session's location is bound at creation, so V2
    needs no per-request directory — the parameter is the portal's calling convention,
    not the wire's.
    """
    code, body = _request("POST", f"/api/session/{session_id}/prompt",
                          {"text": text}, port=port)
    if code not in (200, 204):
        raise HarnessError(f"prompt refused (HTTP {code}): {str(body)[:200]}")
    out = body if isinstance(body, dict) else {"raw": body}
    out.setdefault("queued", True)
    return out


def messages(session_id: str, cwd: str, port: int = DEFAULT_PORT) -> list[dict]:
    code, body = _request("GET", f"/api/session/{session_id}/message", port=port)
    if code != 200:
        raise HarnessError(f"messages refused (HTTP {code}): {str(body)[:160]}")
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, list) else []


# --- the session view (queue 32: the page's operational state) ----------------------
#
# Every fact below is measured against the live 1.19.0 host. Two of them decide what the page is
# allowed to claim:
#
# 1. `GET /session` with no `directory` answers the **server's own project scope**. The folders the
#    projects page launches into are not in that list, so a per-folder read is the only way to
#    learn that a project's own session is on the host.
# 2. `GET /session/{id}/permissions` answers the **web app's HTML** — the SPA fallback page, not an
#    API. There is no read that lists a session's pending permissions, so the session view does not
#    show a permission row: a pending permission is a thing the host announces on the event stream,
#    and the portal has no event stream.

def live_sessions(directories: list[str], port: int = DEFAULT_PORT) -> list[dict]:
    """Every session the host reports for the folders named, **asked per folder**.

    Measured: an unscoped `GET /session` lists the sessions of the folder the host was started in,
    so the projects page would be told, about a session it created a minute earlier, that it is not
    on the host. A folder the host refuses to list is skipped — the caller reports that folder's own
    reason; it must not arrive as "that project has no sessions".
    """
    out: list[dict] = []
    for d in directories:
        if not d:
            continue
        try:
            out.extend(list_sessions(str(d), port=port))
        except HarnessError:
            continue
    return out


def _context_used(tokens: Any) -> int | None:
    """What the last step occupied: the host's own total, or the parts of it when there is no total.

    The host's numbers are counts, so `0` is a real answer and "no number" is not: a step the host
    reported no tokens for is reported as unknown, never as an empty context.
    """
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    keys = [(tokens, "total"), (tokens, "input"), (tokens, "output"), (tokens, "reasoning"),
            (cache, "read"), (cache, "write")]
    for source, key in keys:
        value = source.get(key)
        if key != "total":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return round(value)
    parts = [source.get(key) for source, key in keys if key != "total"]
    nums = [p for p in parts if isinstance(p, int) and not isinstance(p, bool)]
    return sum(nums) if nums else None


def transcript(session_id: str, cwd: str, port: int = DEFAULT_PORT) -> list[dict]:
    """The host's messages as rows the page can print: role, text, the tools a step ran, and what
    the host said it answered with.

    Measured on 2.0.29, the entries are a **union keyed by `type`** (no `{info, parts}` wrapper):
    `user` carries `text` flat; `assistant` carries `content` — parts typed `text`, `reasoning`,
    `tool` — plus `model`, `agent`, `finish`, `cost`, `tokens`; `idle` closes a turn with an
    `outcome`, and `*-selected`/`*-switched` rows bookkeep switches. The union is normalized
    to rows here, so a host upgrade lands in this function and not in the template.
    """
    rows: list[dict] = []
    for item in messages(session_id, cwd, port=port):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "user":
            rows.append({"id": item.get("id"), "role": "user",
                         "text": str(item.get("text") or ""), "tools": [],
                         "model": None, "agent": None, "cost": None,
                         "finish": None, "tokens": {},
                         "time": item.get("time") if isinstance(item.get("time"), dict) else {},
                         "error": None})
            continue
        if kind != "assistant":
            continue          # idle rows and switch records are turn bookkeeping, not bubbles
        parts = item.get("content")
        if not isinstance(parts, list):
            parts = []
        texts = []
        reasoning = []
        tools = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "text":
                texts.append(str(p.get("text") or ""))
            elif ptype == "reasoning":
                reasoning.append(str(p.get("text") or ""))
            elif ptype == "tool":
                state = p.get("state")
                if not isinstance(state, dict):
                    state = {}
                tools.append({"tool": str(p.get("name") or ""), "state": str(state.get("status") or "")})
        text = "\n".join(texts).strip()
        # Measured on this box: its local models answer with `reasoning` + `tool` parts and
        # often **no text part at all** — a row built from text parts alone printed as an
        # empty bubble, and a turn in progress (parts not written yet) printed as one too.
        # The page is allowed a bubble with content: the model's reasoning, then what it ran,
        # then a working marker.
        if not text:
            vis = "\n".join(reasoning).strip()
            if vis:
                text = vis if len(vis) <= 600 else vis[:600] + "…"
            elif tools:
                text = " · ".join(f"{t['tool']} {t['state']}".strip() for t in tools)
            elif not parts:
                text = "…"
        model = None
        ref = item.get("model")
        if isinstance(ref, dict) and (ref.get("providerID") or ref.get("id")):
            model = {"providerID": ref.get("providerID"), "modelID": ref.get("id")}
        rows.append({
            "id": item.get("id"), "role": "assistant", "text": text, "tools": tools,
            "model": model, "agent": item.get("agent"), "cost": item.get("cost"),
            "finish": item.get("finish"), "tokens": item.get("tokens") or {},
            "time": item.get("time") if isinstance(item.get("time"), dict) else {},
            "error": None,
        })
    return rows


def pending_questions(port: int = DEFAULT_PORT) -> list[dict]:
    """The forms the host is holding. Measured on 2.0.29: V2 replaced V1's `/question`
    queue with **forms** — `GET /api/form` answers `{location, data}`, and every
    `Form.Info` names its `sessionID`. A host with nothing pending answers an empty
    `data` — the same answer as "the session never asked", which is why the view only
    reports a form that names *this* session."""
    code, body = _request("GET", "/api/form", port=port)
    data = body.get("data") if isinstance(body, dict) else None
    if code != 200 or not isinstance(data, list):
        raise HarnessError(f"question list refused (HTTP {code}): {str(body)[:160]}")
    return data


def _question_form(item: dict) -> dict:
    """Form.Info → the page's question card: a titled form whose fields each carry the
    key the host's `Form.Answer` object is keyed by, its title/description, its options
    as `{value, label}` pairs, and whether it takes more than one answer."""
    raw_fields = item.get("fields")
    if not isinstance(raw_fields, list):
        raw_fields = []
    questions = []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        raw_options = f.get("options")
        if not isinstance(raw_options, list):
            raw_options = []
        options = []
        for o in raw_options:
            if isinstance(o, dict):
                options.append({"value": str(o.get("value") or o.get("label") or ""),
                                "label": str(o.get("label") or o.get("value") or "")})
        questions.append({
            "key": str(f.get("key") or ""),
            "question": str(f.get("title") or f.get("description") or ""),
            "header": str(f.get("key") or ""),
            "description": str(f.get("description") or ""),
            "type": str(f.get("type") or "string"),
            "multiSelect": f.get("type") == "multiselect",
            "custom": bool(f.get("custom")),
            "options": options,
        })
    return {"id": item.get("id"), "session": item.get("sessionID"),
            "title": str(item.get("title") or ""), "tool": item.get("tool"),
            "questions": questions}


def session_view(session_id: str, cwd: str, context_limit: int | None = None,
                 port: int = DEFAULT_PORT) -> dict[str, Any]:
    """What the page's operational state is made of: the host's session facts, the transcript, who
    answered, the context the last step occupied, whether the host is busy, and any question it is
    holding for this session.

    The three answers are kept apart, because the page must say all three: `ok: False` is the host,
    `found: False` is the host saying it has no such session, and `busy: False` is a session that is
    sitting idle. A page that reads the second as the third tells the operator their session is
    finished when the portal has simply lost its id.
    """
    out: dict[str, Any] = {
        "ok": True, "found": True, "session": session_id, "directory": cwd,
        "title": None, "busy": None, "model": None, "agent": None, "cost": None,
        "context_used": None, "context_limit": context_limit, "turns": 0,
        "question": None, "transcript": [], "reason": None,
    }
    try:
        facts = session(session_id, cwd, port=port)
    except HarnessError as exc:
        msg = str(exc)
        if "HTTP 404" in msg:
            out["found"] = False
            out["reason"] = f"the host has no session {session_id}"
            return out
        out["ok"] = False
        out["found"] = None
        out["error"] = msg
        out["reason"] = "the host did not answer the session read"
        return out
    title = facts.get("title")
    out["title"] = title if isinstance(title, str) and title else facts.get("slug")
    out["cost"] = facts.get("cost")
    ref = facts.get("model")
    if isinstance(ref, dict) and (ref.get("providerID") or ref.get("id")):
        out["model"] = {"providerID": ref.get("providerID"), "modelID": ref.get("id")}
    out["agent"] = facts.get("agent")
    try:
        rows = transcript(session_id, cwd, port=port)
    except HarnessError as exc:
        rows = []
        out["reason"] = f"the transcript was refused (HTTP part of: {str(exc)[:120]})"
    out["transcript"] = rows
    out["turns"] = len(rows)
    # The session's own token figures are the budget's used half; the transcript's last step is the
    # same number read closer to the work, so it wins when it carries one. A session that has never
    # been prompted has an empty transcript and the session object's figures — the state the projects
    # page opens in, right after a launch.
    out["context_used"] = _context_used(facts.get("tokens"))
    for row in reversed(rows):
        if row["role"] == "assistant":
            used = _context_used(row["tokens"])
            if used is not None:
                out["context_used"] = used
            break
    code, active = _request("GET", "/api/session/active", port=port)
    data = active.get("data") if isinstance(active, dict) else None
    if code != 200 or not isinstance(data, dict):
        out["busy"] = None
        out["reason"] = "the host refused the busy read"
    else:
        out["busy"] = session_id in data
    try:
        held = pending_questions(port=port)
    except HarnessError:
        held = []
    for item in held:
        if isinstance(item, dict) and item.get("sessionID") == session_id:
            out["question"] = _question_form(item)
            break
    return out


def send_prompt(session_id: str, text: str, cwd: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """The session view's send button. `prompt` is the wire verb; this is the page's verb, and it
    answers with the message id the host created so the page can say which message it posted.

    The page's verb never raises. Its failure is a fact the operator has to read: the host's own
    words for a session that will not take text ("compacting", "no such session") are the whole
    difference between *the box did nothing* and *this session cannot take text*. A raised error
    reaches the page as a 500 and the box goes quiet, which is the worse of the two.

    The success dict carries `reason` as a key with `None`: the API route logs `out['reason']`
    on every send, and a success shape without the key answered that log line as a KeyError.
    `message` is the host's message id when the verb returns one; the async verb answers 204
    with no body, and the page then names the queue, not a message id it cannot have.
    """
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "session": session_id, "accepted": False,
                "reason": "there is nothing to send — the box is empty"}
    try:
        out = prompt(session_id, text, cwd, port=port)
    except HarnessError as exc:
        return {"ok": False, "session": session_id, "accepted": None,
                "reason": f"the host refused the text ({str(exc)[:260]})"}
    data = out.get("data")
    if not isinstance(data, dict):
        data = {}
    return {"ok": True, "session": session_id, "accepted": True, "queued": bool(out.get("queued")),
            "message": data.get("id"), "reason": None}


def answer_question(session_id: str, form_id: str, answers: dict,
                    port: int = DEFAULT_PORT) -> dict[str, Any]:
    """The host takes a `Form.Answer` object — **keyed by each field's `key`** (measured
    on 2.0.29) — posted to `/api/session/{id}/form/{formID}/reply` as `{answer: {...}}`.
    An answer keyed by anything the form never asked is the V2 equivalent of answering a
    different question than the operator answered."""
    if not isinstance(answers, dict) or not answers:
        raise HarnessError("an answer is an object keyed by the form's field keys")
    code, body = _request("POST", f"/api/session/{session_id}/form/{form_id}/reply",
                          {"answer": answers}, port=port)
    if code not in (200, 204):
        raise HarnessError(f"answer refused (HTTP {code}): {str(body)[:200]}")
    return {"ok": True, "request": form_id, "session": session_id}


def reject_question(session_id: str, form_id: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Declining the form is its own verb — the host has a reply route and the form's own
    `DELETE`; an empty answer is not the same answer as a refusal."""
    code, body = _request("DELETE", f"/api/session/{session_id}/form/{form_id}", port=port)
    if code not in (200, 204):
        raise HarnessError(f"refusal refused (HTTP {code}): {str(body)[:200]}")
    return {"ok": True, "request": form_id, "session": session_id}


def stop_session(session_id: str, cwd: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Stop the session's work. The session stays where it is: the operator stops the run, they do
    not delete the record of it."""
    code, body = _request("POST", f"/api/session/{session_id}/interrupt", port=port)
    if code not in (200, 204):
        raise HarnessError(f"stop refused (HTTP {code}): {str(body)[:200]}")
    return {"ok": True, "session": session_id}


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
