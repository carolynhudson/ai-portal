"""Lightweight host metrics for the D1 chart-friendly metrics pane.

All stdlib (no psutil). Values come straight from /proc so nothing extra is
installed. GPU metrics are best-effort (rocm-smi) and may be ``None``.
"""
from __future__ import annotations

import shutil
import subprocess
import time

from .core import paths

_CPU_PREV = None
_GPU_CACHE = (0.0, None)
_GPU_TTL = 5.0


def _read_cpu():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                return sum(parts), idle
    return None, None


def cpu_percent() -> float | None:
    """Aggregate CPU % since the previous call (first call returns 0.0)."""
    global _CPU_PREV
    total, idle = _read_cpu()
    if total is None:
        return None
    if _CPU_PREV is None:
        _CPU_PREV = (total, idle)
        return 0.0
    ptotal, pidle = _CPU_PREV
    _CPU_PREV = (total, idle)
    dt, di = total - ptotal, idle - pidle
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1 - di / dt)))


def memory() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            vals = rest.split()
            if vals:
                info[k.strip()] = int(vals[0]) * 1024  # kB -> bytes
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {
        "total": total,
        "available": avail,
        "used": total - avail,
        "percent": (100.0 * (total - avail) / total) if total else None,
    }


def load() -> dict:
    with open("/proc/loadavg") as f:
        p = f.read().split()
    return {"1": float(p[0]), "5": float(p[1]), "15": float(p[2])}


def disk() -> dict:
    out = {}
    for label, p in (("root", "/"), ("ai", str(paths.AI_ROOT))):
        try:
            u = shutil.disk_usage(p)
            out[label] = {
                "total": u.total, "used": u.used, "free": u.free,
                "percent": (100.0 * u.used / u.total) if u.total else None,
            }
        except Exception:  # noqa: BLE001
            out[label] = None
    return out


def gpu() -> dict | None:
    """Best-effort AMD GPU usage via rocm-smi. Cached ~5 s. May be None."""
    global _GPU_CACHE
    now = time.time()
    if now - _GPU_CACHE[0] < _GPU_TTL:
        return _GPU_CACHE[1]
    data: dict | None = None
    try:
        out = subprocess.run(["rocm-smi", "--showuse", "--showmeminfo", "vram"],
                             capture_output=True, text=True, timeout=8)
        raw = (out.stdout or "") + (out.stderr or "")
        if out.returncode == 0 and raw.strip():
            data = _parse_rocmsmi(raw)
            data["available"] = True
    except Exception:  # noqa: BLE001
        data = None
    _GPU_CACHE = (now, data)
    return data


def _parse_rocmsmi(out: str) -> dict:
    res = {"available": True, "use_percent": None, "vram_used": None, "vram_total": None, "raw": out.strip()[:500]}
    for line in out.splitlines():
        low = line.lower()
        if "gpu use" in low and "use_percent" not in low:
            nums = _nums(line)
            if nums:
                res["use_percent"] = nums[0]
    return res


def _nums(line: str) -> list[float]:
    out = []
    for tok in line.replace(",", " ").split():
        try:
            out.append(float(tok.strip("%")))
        except ValueError:
            continue
    return out


def snapshot() -> dict:
    return {
        "ts": time.time(),
        "cpu_percent": cpu_percent(),
        "load": load(),
        "memory": memory(),
        "disk": disk(),
        "gpu": gpu(),
    }
