"""ComfyUI provider.

ComfyUI is a git repo + a Python venv together at ~/ai/comfyui (finding F8).
Updates are git + venv sync — there is NO compile step (F8).

Phase E adds the service layer: a static ``ai-comfyui`` user unit whose launch
parameters come from a portal-generated env file (see core.serviceenv), plus
the model inventory and portal-side downloads that place files into the right
``models/<category>/`` folder — no browser downloads, no manual copying.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import Provider, ToolStatus
from ..core import paths, serviceenv, systemd

# Launch parameters, stored at ~/ai/config/comfyui.json. The env file the
# service reads is *generated* from this; changing the json (via the API or by
# hand) + restart is the whole reconfiguration story.
DEFAULT_CONFIG = {"listen": "127.0.0.1", "port": 8188, "extra_args": ""}
CONFIG_FILE = paths.CONFIG / "comfyui.json"


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

    # ------------------------------------------------------------- status
    def status(self) -> ToolStatus:
        local = _git(paths.COMFY_REPO, "rev-parse", "HEAD")
        st = systemd.unit_state(paths.COMFY_UNIT)
        loaded = st.get("LoadState") == "loaded"
        svc_state = st.get("ActiveState", "unknown")
        detail = {
            "repo": str(paths.COMFY_REPO),
            "venv": _venv_info(),
            "service_installed": loaded,
            "config": self.config(),
        }
        return ToolStatus(
            key=self.key, name=self.name, kind=self.kind,
            installed=f"commit {local[:9]}" if local else None,
            deployed=None,
            service_name=paths.COMFY_UNIT if loaded else None,
            service_state=svc_state if loaded else "no-service",
            healthy=bool(local),
            detail=detail,
        )

    # -------------------------------------------------- launch config
    def config(self) -> dict:
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
        except (OSError, ValueError):
            return dict(DEFAULT_CONFIG)

    @staticmethod
    def validate_config(raw: dict) -> dict:
        listen = str(raw.get("listen") or "127.0.0.1").strip()
        if not listen:
            raise ValueError("listen address is empty")
        port_raw = raw.get("port")
        if port_raw is None:
            port_raw = 8188
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise ValueError(f"port must be an integer, got {raw.get('port')!r}")
        if not (1 <= port <= 65535):
            raise ValueError(f"port out of range: {port}")
        extra = " ".join(str(raw.get("extra_args") or "").split())
        return {"listen": listen, "port": port, "extra_args": extra}

    @staticmethod
    def launch_args(cfg: dict) -> str:
        parts = [f"--listen {cfg['listen']}", f"--port {cfg['port']}"]
        if cfg.get("extra_args"):
            parts.append(cfg["extra_args"])
        return " ".join(parts)

    @staticmethod
    def env_mapping(cfg: dict) -> dict[str, str]:
        return {
            "COMFY_PYTHON": str(paths.COMFY_VENV / "bin" / "python"),
            "COMFY_MAIN": str(paths.COMFY_REPO / "main.py"),
            "COMFY_ARGS": ComfyUI.launch_args(cfg),
        }

    def set_config(self, cfg: dict) -> dict:
        """Persist validated config and regenerate the service env file."""
        cfg = self.validate_config(cfg)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
        serviceenv.write_env(paths.COMFY_UNIT_NAME, self.env_mapping(cfg),
                             log=lambda m: None)
        return cfg

    def ensure_service(self, log=print) -> dict:
        """Generate env file + wrapper + static unit (all idempotent).

        Does NOT start the service — the portal's service controls do that.
        """
        cfg = self.validate_config(self.config())
        serviceenv.write_env(paths.COMFY_UNIT_NAME, self.env_mapping(cfg), log)
        serviceenv.write_wrapper(
            paths.COMFY_UNIT_NAME,
            "#!/bin/sh\n"
            "# AI portal — ComfyUI launch wrapper (generated).\n"
            "# All args come from the env file; change them via the portal,\n"
            "# not here.\n"
            'exec "$COMFY_PYTHON" "$COMFY_MAIN" $COMFY_ARGS\n', log)
        serviceenv.install_unit(paths.COMFY_UNIT, paths.COMFY_UNIT_NAME,
                                "ComfyUI (AI Portal)", str(paths.COMFY_REPO), log)
        return {
            "unit": paths.COMFY_UNIT,
            "env": str(serviceenv.env_path(paths.COMFY_UNIT_NAME)),
            "wrapper": str(serviceenv.wrapper_path(paths.COMFY_UNIT_NAME)),
            "config": cfg,
        }

    # --------------------------------------------------- model inventory
    @staticmethod
    def categories() -> list[str]:
        """The model subfolders ComfyUI searches (the real, on-disk ones)."""
        if not paths.COMFY_MODELS.is_dir():
            return []
        return sorted(d.name for d in paths.COMFY_MODELS.iterdir()
                      if d.is_dir() and not d.name.startswith((".", "_")))

    def inventory(self) -> list[dict]:
        """models/<category>/<file> — per-category count, size, and files."""
        out = []
        for cat in self.categories():
            d = paths.COMFY_MODELS / cat
            files = [{"name": f.name, "size": f.stat().st_size,
                      "mtime": f.stat().st_mtime}
                     for f in sorted(d.iterdir())
                     if f.is_file() and not f.name.startswith(".")]
            if not files:
                continue
            out.append({"category": cat, "count": len(files),
                        "size": sum(f["size"] for f in files),
                        "files": files})
        return out

    # ------------------------------------------------- portal downloads
    @staticmethod
    def download_target(url: str, category: str,
                        filename: str | None) -> tuple[Path, Path]:
        """Resolve (dest_dir, dest_file) for a download; raises ValueError on
        unknown category, bad filename, or an already-present target file."""
        cat = (category or "").strip()
        if not cat or "/" in cat or cat.startswith((".", "_")):
            raise ValueError(f"invalid model category: {category!r}")
        dest_dir = paths.COMFY_MODELS / cat
        if not dest_dir.is_dir():
            raise ValueError(f"unknown model category {cat!r} "
                             f"(known: {', '.join(ComfyUI.categories())})")
        name = (filename or "").strip() or (
            (urlparse(url).path or "/").rstrip("/").rsplit("/", 1)[-1])
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"bad filename derived from url: {name!r}")
        dest = dest_dir / name
        if dest.exists():
            raise ValueError(f"{dest.name} already exists in {cat}/")
        return dest_dir, dest

    @staticmethod
    def download_file(url: str, dest_dir, dest, log) -> dict:
        """Stream a URL to ``dest`` via a .part file; rename when complete.
        A crash mid-download never leaves a half-file masquerading as a model."""
        tmp = dest.with_name(dest.name + ".part")
        log(f"downloading {url}")
        done = 0
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total and done // (64 * 1024 * 1024) > (done - len(chunk)) // (64 * 1024 * 1024):
                        log(f"  {done / 1e6:.0f} MB of {total / 1e6:.0f} MB")
        tmp.rename(dest)
        log(f"placed {dest.name} in {dest_dir.name}/ "
            f"({dest.stat().st_size / 1e6:.1f} MB)")
        return {"path": str(dest), "bytes": dest.stat().st_size}
