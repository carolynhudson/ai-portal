"""ComfyUI provider.

ComfyUI is a git repo + a Python venv together at ~/ai/comfyui (finding F8).
Updates are git + venv sync — there is NO compile step (F8). No systemd service
exists yet (added in Phase E); status reports the repo/venv state and whether a
service is present.
"""
from __future__ import annotations

import subprocess

from .base import Provider, ToolStatus
from ..core import paths, systemd


def _venv_info() -> dict:
    py = paths.COMFY_VENV / "bin" / "python"
    info: dict = {}
    if not py.exists():
        return {"error": "venv missing"}
    try:
        out = subprocess.run(
            [str(py), "-c",
             "import sys, torch; print(sys.version.split()[0]); print(torch.__version__)"],
            capture_output=True, text=True, timeout=25).stdout.split()
        if len(out) >= 2:
            info["python"], info["torch"] = out[0], out[1]
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


def _git(repo: str, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=15).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


class ComfyUI(Provider):
    key, name, kind = "comfyui", "ComfyUI", "comfyui"

    def status(self) -> ToolStatus:
        local = _git(paths.COMFY_REPO, "rev-parse", "HEAD")
        st = systemd.unit_state(paths.COMFY_UNIT)
        loaded = st.get("LoadState") == "loaded"
        svc_state = st.get("ActiveState", "unknown")
        detail = {
            "repo": str(paths.COMFY_REPO),
            "venv": _venv_info(),
            "service_installed": loaded,
        }
        # healthy == repo present (a service does not exist yet, so we don't gate on it)
        return ToolStatus(
            key=self.key, name=self.name, kind=self.kind,
            installed=f"commit {local[:9]}" if local else None,
            deployed=None,
            service_name=paths.COMFY_UNIT if loaded else None,
            service_state=svc_state if loaded else "no-service",
            healthy=bool(local),
            detail=detail,
        )
