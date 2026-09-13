"""systemd *user* service helpers + small host probes (all read-only).

The portal owns the AI stack as user services (finding F9). The router
(``ai-llama-router``) is the one this session may be riding on — any stop or
cutover of it is a confirmed, user-visible operation (docs/PORTAL_PLAN.md §6).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import logsetup, paths


def _user(*args: str) -> str:
    try:
        p = subprocess.run(["systemctl", "--user", *args],
                           capture_output=True, text=True, timeout=15)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"(systemctl error: {e})"


def unit_state(name: str) -> dict:
    """Return the state of a user unit. ``load_state`` is 'not-found' if absent."""
    out = _user("show", name,
                "--property=ActiveState,SubState,MainPID,Since,LoadState,UnitFileState")
    d = {"name": name, "ActiveState": "unknown", "LoadState": "not-found"}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def journal_tail(name: str, lines: int = 140) -> list[str]:
    """Raw message lines from a user unit's journal (oldest -> newest).

    Uses ``journalctl --user`` directly (not ``systemctl``) so it works for any
    user service. Best-effort: any failure (unit absent, no journal access,
    timeout) yields ``[]`` rather than raising, so the dashboard degrades
    cleanly instead of 500-ing.
    """
    try:
        p = subprocess.run(
            ["journalctl", "--user", "-u", name, "-n", str(lines),
             "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=8)
        return [ln for ln in p.stdout.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _needs_restarting() -> str | None:
    try:
        p = subprocess.run(["needs-restarting", "-r"],
                           capture_output=True, text=True, timeout=15)
        return (p.stdout or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def reboot_pending() -> dict:
    marker = Path("/var/run/reboot-required")
    pkgs = Path("/var/run/reboot-required.pkgs")
    return {
        "reboot_required": marker.exists(),
        "packages": pkgs.read_text().splitlines() if pkgs.exists() else [],
        "needs_restarting": _needs_restarting(),
    }


def service_memory(unit: str) -> dict | None:
    """RAM for a user service from cgroup v2, bytes: ``current`` + ``peak``.

    A service's cgroup aggregates its child processes, so for the router this
    is the whole llama stack (router + spawned workers) — the real footprint.
    Best-effort: ``None`` if the cgroup files are absent (unit stopped,
    cgroup v1, no permission).
    """
    uid = os.getuid()
    # User-manager units land in app.slice under the user@uid.service cgroup.
    base = (Path("/sys/fs/cgroup/user.slice") / f"user-{uid}.slice"
            / f"user@{uid}.service" / "app.slice" / f"{unit}.service")
    try:
        current = int((base / "memory.current").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        peak = int((base / "memory.peak").read_text().strip())
    except (OSError, ValueError):
        peak = None
    return {"current": current, "peak": peak}


# Units the portal may control (F9). ``portal`` is included so the Settings
# page can restart itself after a bind/port change (Phase I) — restart only,
# never stop, and the UI fires the request without waiting for the response,
# since this process is what answers it (PORTAL_PLAN §4.3).
MANAGED_UNITS = {
    "llamacpp": {
        "unit": "ai-llama-router",
        "name": "llama.cpp router",
        "confirm": ("STOPPING THE ROUTER DROPS EVERY AGENT SESSION RIDING IT "
                    "(including any AI agent in use right now). Continue?"),
    },
    "comfyui": {
        "unit": "ai-comfyui",
        "name": "ComfyUI",
        "confirm": None,
    },
    "portal": {
        "unit": "ai-portal",
        "name": "AI Portal",
        "confirm": ("Restart the portal itself? The page will reconnect in a "
                    "moment."),
    },
}


def _service_rollback_note(unit: str, action: str) -> None:
    """Pre-stop/restart snapshot under state/rollback/ (phase-4 pattern)."""
    d = paths.ROLLBACK_DIR / f"service-{unit}-{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state-before.txt").write_text(repr(unit_state(unit)) + "\n")
    (d / "action.txt").write_text(f"{action} {unit} @ {time.ctime()}\n")


def service_action(key: str, action: str) -> dict:
    """start/stop/restart a managed user unit. Returns ok + new state.

    stop/restart write a rollback note first (§6 safety model).
    """
    meta = MANAGED_UNITS.get(key)
    if meta is None:
        return {"ok": False, "error": "unknown service"}
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": "unknown action"}
    unit = meta["unit"]
    log = logsetup.get("systemd")
    if action in ("stop", "restart"):
        _service_rollback_note(unit, action)
    log.info("%s %s …", action, unit)
    try:
        p = subprocess.run(["systemctl", "--user", action, unit],
                           capture_output=True, text=True, timeout=90)
    except Exception as e:  # noqa: BLE001
        log.error("%s %s failed: %s", action, unit, e)
        return {"ok": False, "error": f"systemctl error: {e}"}
    state = unit_state(unit)
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip()[:400]
        log.error("%s %s failed (%s): %s", action, unit, p.returncode, err)
        return {"ok": False, "state": state, "error": err}
    log.info("%s %s -> %s/%s", action, unit, state.get("ActiveState"),
             state.get("SubState"))
    return {"ok": True, "state": state}
