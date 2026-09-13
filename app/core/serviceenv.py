"""Manage the shared runtime environment file (~/ai/config/ai-env.env).

All three portal-managed user units (ai-llama-router, ai-comfyui, ai-portal)
reference this single env file so a change in one place propagates to every
service after a restart. The Settings page will expose read/write for the
key variables (LD_LIBRARY_PATH today; more later).

Changing the file does NOT auto-restart services — the caller must explicitly
restart each unit via systemd.service_action() for the new values to take effect.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paths

_ENV_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def read_env() -> dict[str, str]:
    """Parse ai-env.env into {KEY: VALUE}. Missing file → {}."""
    f = paths.AI_ENV_FILE
    if not f.exists():
        return {}
    out: dict[str, str] = {}
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_RE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
            out[key] = val
    return out


def write_env(env: dict[str, str]) -> None:
    """Write {KEY: VALUE} back to ai-env.env with a header comment."""
    f = paths.AI_ENV_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Shared runtime environment for all AI portal-managed user services.",
        "# Referenced by ai-llama-router, ai-comfyui, and ai-portal systemd units.",
        "# Managed by the AI Portal Settings page. Edits require a service restart.",
        "",
    ]
    for key, val in env.items():
        q = '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"{key}={q}")
    f.write_text("\n".join(lines) + "\n")


def set_var(key: str, value: str) -> dict[str, str]:
    """Set a single variable; returns the full updated env dict."""
    env = read_env()
    env[key] = value
    write_env(env)
    return env


def get_var(key: str) -> str | None:
    return read_env().get(key)
