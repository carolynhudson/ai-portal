"""What the router is, as opposed to what the portal hopes it is (§6.14.1l).

The portal controls one unit — `ai-llama-router`, a `llama-server` in multi-model
mode that spawns a worker process per model it loads. That is two kinds of process
the operator cares about, and neither of them is the portal. A third kind exists and
the portal does not control it at all: a `llama-server` somebody started by hand.

Why that is worth a page section is a specific failure, not a general tidiness rule.
A hand-started server sitting on the router's port means `systemctl start` "succeeds"
— systemd is happy, the unit is active — and the router it started cannot answer on
the port every client is configured to reach. The portal would report `active` and the
operators would report nothing. A second server holding six gigabytes of GPU is the
same story with a different symptom: the router starts, loads a model, and the two
compete for memory.

So this module answers three questions and none of them is "is the unit active":

1. **Which processes are the router's?** The unit's `MainPID`, and the children of
   that pid — the workers are spawned by the router itself, so parentage is the
   definition, not a heuristic.
2. **Which `llama-server` processes are nobody's?** Anything running that binary that
   is neither the unit's main process nor its child. The portal cannot restart, stop
   or reason about these, and the page has to say so instead of offering buttons for
   them.
3. **What is the router's own account of itself?** The unit's command line (the port,
   the preset file, `--models-max`), the workers' aliases and ports, and the served
   model list when the router answers. The portal renders `presets.ini`; the router
   reads it at launch. Checking those against each other is the entire value of the
   panel.

Read-only, like every other probe in this app: `start`/`stop`/`restart` stay in
:func:`app.core.systemd.service_action`, behind their confirms. Nothing here touches a
process.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Sequence

from . import paths, systemd

# The binary names a model server runs as. The router unit and a hand-started server
# are both `llama-server`; the worker is too. The distinction is parentage, not the
# name, so this list is only the filter that decides which processes are interesting.
SERVER_NAMES = ("llama-server", "llama-llm-server", "llama-rpc-server")

# `--port` is the only port flag a llama-server takes; `-p` is `--parallel`, so it is
# deliberately absent here: a hand-started server's `--parallel 2` must not be read as a
# port. `[\s=]` covers both `--port 8080` and `--port=8080`, because the port is the
# number this module's whole point depends on and people spell it both ways.
_PORT_RE = re.compile(r"--port[\s=]+(?P<port>\d{2,5})")
_ALIAS_RE = re.compile(r"--alias[\s=]+(?P<alias>[^\s]+)")
_PRESET_RE = re.compile(r"--models-preset[\s=]+(\S+)")
_MODELS_MAX_RE = re.compile(r"--models-max[\s=]+(\d+)")


def _ps_lines() -> list[str]:
    """Every process on the box as `pid ppid rss cmd`, one per line.

    `ps` is the tool, not `/proc` walking: the command line is what this module is
    about, and `ps` gives pid, parent and memory in one call with the argv already
    flattened. A test injects its own text, so the parsing below is exercised
    everywhere and the real `ps` only in the live checks.
    """
    p = subprocess.run(["ps", "-eo", "pid=,ppid=,rss=,comm=,args="],
                       capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:200] or "ps failed")
    return p.stdout.splitlines()


def _is_server(comm: str) -> bool:
    return any(comm == n or comm.endswith("/" + n) for n in SERVER_NAMES)


def parse_processes(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Turn `ps` rows into records. Public because the classification is the product."""
    out: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        comm, cmd = parts[3], parts[4]
        if not _is_server(comm):
            continue
        ports: list[int] = []
        for m in _PORT_RE.finditer(cmd):
            ports.append(int(m.group("port")))
        alias = _ALIAS_RE.search(cmd)
        out.append({
            "pid": pid, "ppid": ppid, "rss_kib": rss, "cmd": cmd,
            "ports": sorted(set(ports)),
            "alias": alias.group("alias") if alias else None,
        })
    return out


def unit_main_pid(unit: str = "") -> int | None:
    """The unit's main pid, or None when it is not running.

    `MainPID=0` is systemd's answer for an inactive unit, so zero is reported as
    None: "no main process" is the fact the caller needs, and 0 is the pid of the
    kernel, which is not a fact anyone can act on.
    """
    unit = unit or paths.ROUTER_UNIT
    try:
        out = systemd._user("show", unit, "-p", "MainPID")
    except Exception:  # noqa: BLE001
        # A unit we cannot ask about has no main process, which is the same answer as an
        # inactive one, and it is the safe direction to be wrong in: every server found
        # then reads as foreign, so the page over-reports what it cannot account for.
        return None
    m = re.search(r"^MainPID=(\d+)$", out, re.M)
    if not m:
        return None
    pid = int(m.group(1))
    return pid or None


def classify(main_pid: int | None, procs: list[dict[str, Any]]) -> dict[str, Any]:
    """Split the servers into the router, its workers, and everything else."""
    router = next((p for p in procs if main_pid and p["pid"] == main_pid), None)
    workers, foreign = [], []
    for p in procs:
        if router and p["pid"] == router["pid"]:
            continue
        if router and p["ppid"] == router["pid"]:
            workers.append(p)
        else:
            foreign.append(p)
    return {"router": router, "workers": workers, "foreign": foreign}


def _launch_facts(cmd: str) -> dict[str, Any]:
    port = _PORT_RE.search(cmd)
    preset = _PRESET_RE.search(cmd)
    maxm = _MODELS_MAX_RE.search(cmd)
    return {
        "port": int(port.group("port")) if port else None,
        "presets": preset.group(1) if preset else None,
        "models_max": int(maxm.group(1)) if maxm else None,
    }


def served_models(port: int | None, timeout: float = 3.0) -> dict[str, Any]:
    """The router's own list of what it serves. Degrades alone (§health's rule)."""
    if not port:
        return {"ok": False, "error": "the router's port is not in its command line"}
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",
                                    timeout=timeout) as r:
            data = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        rows = __import__("json").loads(data).get("data") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"unparseable /v1/models: {e}"}
    loaded = [m for m in rows if (m.get("status") or {}).get("value") == "loaded"]
    aliases = sorted({a for m in rows for a in (m.get("aliases") or [])})
    return {"ok": True, "count": len(rows), "loaded": len(loaded),
            "loaded_ids": [m.get("id") for m in loaded], "aliases": aliases}


_UNSET = object()


def self_report(unit: str = "", ps_lines: Sequence[str] | None = None,
                main_pid: Any = _UNSET) -> dict[str, Any]:
    """The router's account of itself, plus the processes the portal is not responsible for.

    `ps_lines` and `main_pid` are seams for the tests: the classification is the thing under test,
    and a test that had to arrange a real unit and real processes to exercise it would be a test of
    the machine it runs on.
    """
    unit = unit or paths.ROUTER_UNIT
    st = systemd.unit_state(unit)
    state = st.get("ActiveState") or "unknown"
    main = unit_main_pid(unit) if main_pid is _UNSET else main_pid
    report: dict[str, Any] = {
        "unit": unit,
        "state": state,
        "sub_state": st.get("SubState"),
        "main_pid": main,
        "controllable": st.get("LoadState", "not-found") != "not-found",
    }
    try:
        procs = parse_processes(ps_lines if ps_lines is not None else _ps_lines())
    except Exception as e:  # noqa: BLE001
        # The process list is the half of the report that notices a hand-started
        # server. Saying "could not look" is the honest degradation; a page that
        # showed "no foreign processes" here would be claiming it checked.
        report["processes"] = {"ok": False, "error": str(e)}
        return report

    kinds = classify(main, procs)
    router = kinds["router"]
    report["processes"] = {
        "ok": True,
        "router": router and {**router, "launch": _launch_facts(router["cmd"])},
        "workers": [{**w, "launch": _launch_facts(w["cmd"])} for w in kinds["workers"]],
        "foreign": kinds["foreign"],
        "total_rss_kib": sum(p["rss_kib"] for p in procs),
    }
    # Port collisions are the case that makes `systemctl start` lie, so they are
    # computed here rather than left for a person to spot in two lists of numbers.
    router_ports = set((report["processes"]["router"] or {}).get("ports") or [])
    clash = sorted({p for f in kinds["foreign"] for p in f["ports"] if p in router_ports})
    report["processes"]["port_clash"] = clash

    served = served_models((report["processes"]["router"] or {}).get("launch", {}).get("port")
                           if router else None)
    report["served"] = served
    report["presets"] = _presets_facts(report)
    return report


def _presets_facts(report: dict[str, Any]) -> dict[str, Any]:
    """The file the router was told to read, and the file that is on disk."""
    out: dict[str, Any] = {"path": str(paths.PRESETS_INI)}
    launched = ((report.get("processes") or {}).get("router") or {}).get("launch") or {}
    out["launched_with"] = launched.get("presets")
    out["models_max"] = launched.get("models_max")
    try:
        raw = paths.PRESETS_INI.read_bytes()
    except OSError as e:
        out["error"] = str(e)
        return out
    import hashlib
    out["sha256"] = hashlib.sha256(raw).hexdigest()[:12]
    out["sections"] = len(re.findall(r"^\s*\[", raw.decode("utf-8", "replace"), re.M))
    if out["launched_with"] and out["launched_with"] != str(paths.PRESETS_INI):
        out["warning"] = ("the router was launched with a preset file that is not the one "
                          f"the portal renders ({out['launched_with']})")
    return out
