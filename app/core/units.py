"""Per-service launch config: env file + shell wrapper + static systemd unit.

Complements :mod:`serviceenv` (the *shared* ``~/ai/config/ai-env.env`` that
all three units read). Each service additionally gets its own
``~/ai/config/<name>.env`` holding the launch parameters (host, port, args);
when multi-word arguments are involved, a thin ``~/ai/bin/ai-<name>.sh``
wrapper re-splits them into argv words. The systemd unit itself stays static:

    rewrite env file  ->  systemctl --user restart <unit>

No unit edits or daemon-reload on re-parameterisation (daemon-reload only
happens when the unit file itself is (re)installed).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import paths


def env_path(name: str) -> Path:
    """The env file systemd reads for service ``name``."""
    return paths.CONFIG / f"{name}.env"


def wrapper_path(name: str) -> Path:
    """The shell wrapper systemd actually execs for service ``name``."""
    return paths.AI_BIN / f"ai-{name}.sh"


def unit_path(unit: str) -> Path:
    return paths.HOME / ".config" / "systemd" / "user" / f"{unit}.service"


def _quote(v: str) -> str:
    """Double-quote for systemd's EnvironmentFile (escapes \\\\ and \")."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(name: str, mapping: dict[str, str], log=print) -> Path:
    """Write ``KEY=VALUE`` lines. Values are quoted so systemd keeps spaces
    intact; the wrapper decides which variables to re-split."""
    p = env_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"{k}={_quote(v)}\n" for k, v in mapping.items()))
    log(f"wrote {p} ({len(mapping)} keys)")
    return p


def write_wrapper(name: str, body: str, log=print) -> Path:
    p = wrapper_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)
    log(f"wrote {p}")
    return p


def install_unit(unit: str, service: str, description: str,
                 working_dir: str, log=print) -> None:
    """Write the static unit (shared ai-env.env + per-service env file) and
    daemon-reload so systemd sees it."""
    p = unit_path(unit)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[Unit]\n"
        f"Description={description}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"EnvironmentFile={paths.AI_ENV_FILE}\n"
        f"EnvironmentFile={env_path(service)}\n"
        f"ExecStart={wrapper_path(service)}\n"
        f"WorkingDirectory={working_dir}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, timeout=15)
    log(f"installed unit {unit} (daemon-reloaded)")
