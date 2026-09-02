"""llama.cpp provider: release registry, deployed binary, router service, models."""
from __future__ import annotations

import json
import subprocess
import time

from .base import Provider, ToolStatus
from ..core import paths, systemd


def _git_head(repo: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _origin_main(repo: str) -> str | None:
    """HEAD commit of the remote's *default* branch.

    Resolves the default branch via a symref (llama.cpp uses ``master``, not
    ``main``), so the behind-by works regardless of the branch's name.
    """
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-remote", "--symref", "origin", "HEAD"],
                             capture_output=True, text=True, timeout=25)
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        if len(lines) >= 2:
            return lines[1].split()[0] if origin else None
    except Exception:  # noqa: BLE001
        return None


def _router_models() -> list[dict]:
    try:
        import httpx
        r = httpx.get(f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}/v1/models", timeout=8)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:  # noqa: BLE001
        return [{"id": f"(router unreachable: {e})"}]


def _scan_models() -> list[dict]:
    out = []
    root = paths.LLAMA_MODELS
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        ggufs = [f for f in d.glob("*.gguf") if f.is_file()]
        out.append({
            "id": d.name,
            "dir": str(d),
            "files": len(ggufs),
            "size": sum(f.stat().st_size for f in ggufs),
        })
    return out


def _binary_version() -> str | None:
    binary = paths.AI_BIN / "llama-server"
    if not binary.exists():
        return None
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=15)
        line = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
        return line[0] if line else None
    except Exception:  # noqa: BLE001
        return None


_BEHIND_CACHE = (0.0, None)


def behind_by() -> dict:
    """Local repo HEAD vs origin/main. Cached 5 min (git ls-remote needs network)."""
    global _BEHIND_CACHE
    now = time.time()
    if now - _BEHIND_CACHE[0] < 300:
        return _BEHIND_CACHE[1]
    local = _git_head(paths.LLAMA_REPO)
    origin = _origin_main(paths.LLAMA_REPO)
    n = None
    if local and origin:
        try:
            out = subprocess.run(["git", "-C", str(paths.LLAMA_REPO), "rev-list",
                                  "--count", f"{local}..{origin}"],
                                 capture_output=True, text=True, timeout=25)
            n = int(out.stdout.strip()) if out.stdout.strip().isdigit() else None
        except Exception:  # noqa: BLE001
            n = None
    data = {"local": local, "origin": origin, "behind": n}
    _BEHIND_CACHE = (now, data)
    return data


class LlamaCpp(Provider):
    key, name, kind = "llamacpp", "llama.cpp", "llamacpp"

    def status(self) -> ToolStatus:
        detail: dict = {}

        releases, current = [], None
        try:
            reg = json.loads(paths.RELEASES_JSON.read_text())
            current = reg.get("current")
            releases = reg.get("releases", [])
        except Exception as e:  # noqa: BLE001
            detail["releases_error"] = str(e)
        detail["releases"] = releases
        detail["current_release"] = current
        detail["vulkan_sdk"] = str(paths.VULKAN_SDK)

        st = systemd.unit_state(paths.ROUTER_UNIT)
        svc_state = st.get("ActiveState", "unknown")
        local = _git_head(paths.LLAMA_REPO)

        return ToolStatus(
            key=self.key, name=self.name, kind=self.kind,
            installed=f"src @ {local[:9]}" if local else None,
            deployed=_binary_version(),
            service_name=paths.ROUTER_UNIT,
            service_state=svc_state,
            healthy=(svc_state == "active"),
            detail=detail,
        )

    def behind(self) -> dict:
        return behind_by()

    def models_live(self) -> list[dict]:
        return _router_models()

    def models_scan(self) -> list[dict]:
        return _scan_models()
