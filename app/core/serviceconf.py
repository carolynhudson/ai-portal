"""Per-service launch settings (host/port) — read, validate, and update.

Each managed service gets a small env file under ~/ai/config/ that its
systemd unit references; host/port live there so the Settings page can
change them without touching unit files:

    llamacpp.env  -> ai-llama-router.service   (LLAMA_HOST / LLAMA_PORT)
    comfyui.env   -> ai-comfyui.service        (COMFY_HOST / COMFY_PORT)
    portal.env    -> ai-portal.service         (AI_PORTAL_HOST / AI_PORTAL_PORT)

Port validation rules (enforced by :func:`validate_port`):
  1. integer in the valid TCP range 1..65535
  2. not assigned to another managed service (no self-collisions)
  3. not already bound by anything else on this system (ss -tlnH probe;
     the changing service's own current binding is tolerated, since a
     restart re-binds it)
  4. <1024 -> warning only (privileged range; user services cannot bind
     these without extra capability — flagged, not refused)

External access toggle: host "127.0.0.1" = loopback-only (disabled),
host "0.0.0.0" = all interfaces (enabled).

Writes do NOT restart anything — the caller (Settings page) shows a
"restart required" notice and triggers systemd.service_action() per unit.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import paths

# service key -> env file + variable names used by its unit's ExecStart
SERVICE_CONF: dict[str, dict] = {
    "llamacpp": {
        "label": "llama.cpp router",
        "env_file": paths.CONFIG / "llamacpp.env",
        "host_key": "LLAMA_HOST",
        "port_key": "LLAMA_PORT",
        "unit": paths.ROUTER_UNIT,
    },
    "comfyui": {
        "label": "ComfyUI",
        "env_file": paths.CONFIG / "comfyui.env",
        "host_key": "COMFY_HOST",
        "port_key": "COMFY_PORT",
        "unit": paths.COMFY_UNIT,
    },
    "portal": {
        "label": "AI Portal",
        "env_file": paths.CONFIG / "portal.env",
        "host_key": "AI_PORTAL_HOST",
        "port_key": "AI_PORTAL_PORT",
        "unit": paths.PORTAL_UNIT,
    },
}

LOOPBACK = "127.0.0.1"
EXTERNAL = "0.0.0.0"


def _read_env_file(f: Path) -> dict[str, str]:
    if not f.exists():
        return {}
    out: dict[str, str] = {}
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _quote(v: str) -> str:
    """Double-quote for systemd's EnvironmentFile (escapes \\\\ and \")."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_env_file(f: Path, env: dict[str, str]) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {f.stem} launch settings (managed by AI Portal Settings page).", ""]
    for k, v in env.items():
        lines.append(f"{k}={_quote(v)}")
    f.write_text("\n".join(lines) + "\n")


def service_ports() -> dict[str, dict]:
    """Current host/port per managed service (from the env files)."""
    out: dict[str, dict] = {}
    for key, conf in SERVICE_CONF.items():
        env = _read_env_file(conf["env_file"])
        host = env.get(conf["host_key"], LOOPBACK)
        try:
            port = int(env.get(conf["port_key"], "0"))
        except ValueError:
            port = 0
        out[key] = {
            "label": conf["label"],
            "unit": conf["unit"],
            "env_file": str(conf["env_file"]),
            "host": host,
            "port": port,
            "external": host == EXTERNAL,
        }
    return out


def listening_ports() -> set[int]:
    """All TCP ports currently bound on this system (ss -tlnH)."""
    try:
        out = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:  # noqa: BLE001
        return set()
    ports: set[int] = set()
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 4:
            local = parts[3]
            hostpart, _, p = local.rpartition(":")
            if p.isdigit():
                ports.add(int(p))
    return ports


def validate_port(port, service: str) -> dict:
    """Validation result for putting ``service`` on ``port``.

    Returns {"ok": bool, "errors": [...], "warnings": [...]}.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if isinstance(port, bool) or not isinstance(port, int):
        try:
            port = int(str(port).strip())
        except (TypeError, ValueError):
            return {"ok": False, "errors": [f"port must be an integer, got {port!r}"],
                    "warnings": []}

    if not (1 <= port <= 65535):
        errors.append(f"port {port} outside valid TCP range 1-65535")
        return {"ok": False, "errors": errors, "warnings": warnings}
    if port < 1024:
        warnings.append(f"port {port} is in the privileged range (<1024); "
                        "user services cannot bind it without extra capability")

    for other, st in service_ports().items():
        if other != service and st["port"] == port:
            errors.append(f"port {port} is already assigned to the "
                          f"{st['label']} service ({other})")

    bound = listening_ports()
    cur = service_ports().get(service, {}).get("port")
    if port in bound and port != cur:
        errors.append(f"port {port} is already in use by something else on "
                      f"this system")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def set_service_port(service: str, port: int | None = None,
                     external: bool | None = None) -> dict:
    """Validate + persist a host/port change for one service. No restart."""
    if service not in SERVICE_CONF:
        raise ValueError(f"unknown service {service!r} "
                         f"(known: {', '.join(SERVICE_CONF)})")
    conf = SERVICE_CONF[service]
    env = _read_env_file(conf["env_file"])
    cur_host = env.get(conf["host_key"], LOOPBACK)

    new_port = port
    if new_port is None:
        try:
            new_port = int(env.get(conf["port_key"], "0"))
        except ValueError:
            new_port = 0
    new_host = (EXTERNAL if external else LOOPBACK) if external is not None else cur_host

    if port is not None or external is not None:
        vres = validate_port(new_port, service)
        if not vres["ok"]:
            return {"ok": False, "errors": vres["errors"],
                    "warnings": vres["warnings"]}

    env[conf["port_key"]] = str(new_port)
    env[conf["host_key"]] = new_host
    _write_env_file(conf["env_file"], env)
    return {"ok": True, "service": service, "host": new_host,
            "port": new_port, "external": new_host == EXTERNAL,
            "warnings": (validate_port(new_port, service)["warnings"]
                         if port is not None or external is not None else []),
            "needs_restart": True, "unit": conf["unit"]}
