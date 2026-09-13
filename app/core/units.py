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
import time
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


UNIT_TEMPLATE = """[Unit]
Description={description}
After=network.target

[Service]
EnvironmentFile={shared}
EnvironmentFile={env}
ExecStart={wrapper}
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def render_unit(unit: str, service: str, description: str,
                working_dir: str) -> str:
    """The unit text install_unit would write (one source of truth, also used
    by ``python -m app.deploy --print-units``)."""
    return UNIT_TEMPLATE.format(description=description, shared=paths.AI_ENV_FILE,
                               env=env_path(service), wrapper=wrapper_path(service),
                               workdir=working_dir)


def install_unit(unit: str, service: str, description: str,
                 working_dir: str, log=print) -> dict:
    """Write the static unit (shared ai-env.env + per-service env file) and
    daemon-reload so systemd sees it.

    An existing *different* unit file is copied to ``state/rollback/units/``
    first (safety model, PORTAL_PLAN §6): this is how a hand-written unit gets
    replaced by the generated one without losing the only copy — the running
    service is untouched until it is next restarted, so the operator can
    restore the backup and ``daemon-reload`` to undo.
    """
    p = unit_path(unit)
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    new_text = render_unit(unit, service, description, working_dir)
    if p.exists() and p.read_text() != new_text:
        backup = paths.ROLLBACK_DIR / "units" / (
            f"{unit}.service.{time.strftime('%Y%m%d-%H%M%S')}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(p.read_text())
        log(f"backed up previous unit file -> {backup}")
    p.write_text(new_text)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, timeout=15)
    log(f"installed unit {unit} (daemon-reloaded)")
    return {"unit_file": str(p), "backup": str(backup) if backup else None,
            "env_file": str(env_path(service)),
            "wrapper": str(wrapper_path(service))}
