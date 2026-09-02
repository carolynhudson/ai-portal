"""Provider interface.

Every managed tool implements ``status()`` (read-only snapshot). Lifecycle
(start/stop/restart) and ``update()`` are declared here but deliberately not
implemented until their phase; see docs/PORTAL_PLAN.md §4 and §7.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class ToolStatus:
    key: str
    name: str
    kind: str                      # llamacpp | comfyui | huggingface
    installed: str | None = None   # version/commit of source or library
    deployed: str | None = None    # version/commit of the running artifact
    service_name: str | None = None
    service_state: str | None = None   # active | inactive | n/a | unknown
    healthy: bool = False
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Provider:
    key: str = "base"
    name: str = "base"
    kind: str = "base"

    def status(self) -> ToolStatus:  # pragma: no cover - interface
        raise NotImplementedError

    # --- not wired until later phases (kept so the UI can offer them) ---
    def update(self, target: str | None = None, dry: bool = True) -> dict:
        return {
            "status": "not-implemented",
            "note": "update execution lands in a later phase (D3: advisory for now)",
        }

    def _lifecycle(self, action: str) -> dict:
        return {
            "status": "not-implemented",
            "action": action,
            "note": "service lifecycle lands in Phase B; the router is protected "
                    "by the safety model (docs/PORTAL_PLAN.md §6)",
        }

    def start(self) -> dict:
        return self._lifecycle("start")

    def stop(self) -> dict:
        return self._lifecycle("stop")

    def restart(self) -> dict:
        return self._lifecycle("restart")
