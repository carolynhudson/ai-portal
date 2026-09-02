"""systemd *user* service helpers + small host probes (all read-only).

The portal owns the AI stack as user services (finding F9). The router
(``ai-llama-router``) is the one this session may be riding on — any stop or
cutover of it is a confirmed, user-visible operation (docs/PORTAL_PLAN.md §6).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


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
