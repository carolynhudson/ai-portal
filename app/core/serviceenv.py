"""Portal-managed launch config for user services (env file + wrapper).

The systemd unit stays *static*. All launch parameters live in a generated
env file (``~/ai/config/<name>.env``) which a thin shell wrapper
(``~/ai/bin/ai-<name>.sh``) consumes. Re-parameterizing a service is then:

    rewrite env file  ->  systemctl --user restart <unit>

No unit edits, no daemon-reload, and every knob is visible in one small file
the portal owns. We are proving this pattern on ComfyUI (no live traffic)
before porting it to the llama router.

Division of config, once the router adopts this too:

- the **env file** governs launch parameters (host, port, slots, extra flags)
- ``presets.ini`` keeps governing *model slots* (paths, ctx, draft models)

systemd does not word-split env values, so multi-word args (``COMFY_ARGS``)
are written as one quoted value and the wrapper's shell re-splits them when
the variable is left unquoted in the ``exec`` line.
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
    """Double-quote for systemd's EnvironmentFile (escapes \\ and ")."""
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
    """Write the static unit (env file + wrapper are the only moving parts)
    and daemon-reload so systemd sees it."""
    p = unit_path(unit)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[Unit]\n"
        f"Description={description}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
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
