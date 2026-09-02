"""Hugging Face provider.

There is no HF service to manage. The portal manages the *cache*
(~/.cache/huggingface, kept in place per finding F11) — reporting size/repos,
and (in a later phase) running downloads / pruning through huggingface_hub.
"""
from __future__ import annotations

import os
import time

from .base import Provider, ToolStatus
from ..core import paths

_SIZE_CACHE = (0.0, 0)
_SIZE_TTL = 120.0


def _hub_repos() -> list[str]:
    hub = paths.HF_CACHE / "hub"
    if not hub.is_dir():
        return []
    return sorted(p.name for p in hub.iterdir()
                  if p.is_dir() and p.name.startswith("models--"))


def _cache_size() -> int:
    global _SIZE_CACHE
    now = time.time()
    if now - _SIZE_CACHE[0] < _SIZE_TTL:
        return _SIZE_CACHE[1]
    size = 0
    try:
        if paths.HF_CACHE.is_dir():
            for root, _dirs, files in os.walk(paths.HF_CACHE):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    _SIZE_CACHE = (now, size)
    return size


def _hub_version() -> str | None:
    try:
        import huggingface_hub
        return getattr(huggingface_hub, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


class HuggingFace(Provider):
    key, name, kind = "huggingface", "Hugging Face", "huggingface"

    def status(self) -> ToolStatus:
        v = _hub_version()
        hfcli = paths.HOME / ".local" / "bin" / "hf"
        return ToolStatus(
            key=self.key, name=self.name, kind=self.kind,
            installed=f"huggingface_hub {v}" if v else None,
            deployed=None,
            service_name=None, service_state="n/a",
            healthy=paths.HF_CACHE.is_dir(),
            detail={
                "cache": str(paths.HF_CACHE),
                "cache_size": _cache_size(),
                "repos": _hub_repos(),
                "hf_cli": str(hfcli) if hfcli.exists() else None,
            },
        )
