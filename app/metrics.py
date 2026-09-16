"""Lightweight host metrics for the D1 chart-friendly metrics pane.

All stdlib (no psutil). Values come straight from /proc so nothing extra is
installed. GPU metrics are best-effort (rocm-smi) and may be ``None``.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

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


# Where the driver publishes the memory domains directly. On an iGPU (Strix Halo,
# gfx1151) there is no discrete VRAM: rocm-smi reports ~512 MiB "VRAM" while the
# memory the models actually live in is the GTT aperture, here 108 GiB. Reading
# only rocm-smi therefore shows an iGPU box as nearly full and hides the figure
# that decides whether a model fits.
_DRM_CARDS = Path("/sys/class/drm")


def _gpu_dev() -> Path | None:
    """The amdgpu device directory, via /sys/class/drm/cardN/device.

    Resolved through the class symlink rather than globbing a fixed device path:
    this box exposes the GPU as card1, and the real node lives under a PCI path
    (/sys/devices/pci.../0000:c3:00.0) that varies by machine. cardN also has
    connector children (card1-DP-1), so only the bare cardN entries are tried.
    """
    try:
        if not _DRM_CARDS.is_dir():
            return None
        for card in sorted(_DRM_CARDS.iterdir()):
            name = card.name
            if not (name.startswith("card") and name[4:].isdigit()):
                continue
            dev = card / "device"
            if (dev / "mem_info_gtt_total").is_file():
                return dev
    except OSError:
        return None
    return None


def _gtt() -> dict:
    """GTT aperture used/total from the amdgpu driver, in bytes.

    This is the number the §4.7 memory budget has to be planned against, and it
    is also what the `ttm.pages_limit` / `amdttm pages_limit` modprobe fix
    (/etc/modprobe.d/amdttm.conf, 28311552 pages = 108 GiB) actually raises. The
    driver's default caps that aperture at 64 GiB — below the size of the 88 GiB
    model this box serves, and the user hit exactly that as a model that loaded
    but reported only a 4096-token context. Empty dict when amdgpu is absent, so
    the caller can tell "no GPU" from "GPU with zero free".
    """
    out: dict = {}
    dev = _gpu_dev()
    if dev is None:
        return out
    try:
        out["gtt_total"] = int((dev / "mem_info_gtt_total").read_text().strip())
        used = dev / "mem_info_gtt_used"
        if used.is_file():
            out["gtt_used"] = int(used.read_text().strip())
        vt = dev / "mem_info_vram_total"
        if vt.is_file():
            out["vram_total_driver"] = int(vt.read_text().strip())
    except (OSError, ValueError):
        return {}
    return out


def gpu() -> dict | None:
    """AMD GPU usage: rocm-smi for utilisation, driver sysfs for the memory
    domains. Cached ~5 s. May be None."""
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
    # Merge the driver's own numbers in regardless of whether rocm-smi answered:
    # on an iGPU they disagree by ~200x and only sysfs describes reality.
    try:
        g = _gtt()
        if g:
            data = data or {"available": True}
            data.update(g)
            # On a discrete-VRAM card VRAM is the real pool; on an iGPU GTT is.
            # Call whichever is larger the pool the budget must fit into, so the
            # same code is honest on both.
            vram = data.get("vram_total_driver") or 0
            gtt = data.get("gtt_total") or 0
            data["pool_total"] = max(vram, gtt)
            data["pool_used"] = (data.get("gtt_used") or 0) if gtt >= vram else \
                (data.get("vram_used") or 0)
            data["is_igpu"] = gtt > vram
    except Exception:  # noqa: BLE001
        pass
    _GPU_CACHE = (now, data)
    return data


def _parse_rocmsmi(out: str) -> dict:
    res = {"available": True, "use_percent": None, "vram_used": None,
           "vram_total": None, "raw": out.strip()[:500]}
    for line in out.splitlines():
        low = line.lower()
        if "gpu use" in low and "use_percent" not in low:
            nums = _nums(line)
            if nums:
                res["use_percent"] = nums[0]
        # rocm-smi prints "VRAM Total Memory (B): 536870912" — bytes, and on an
        # iGPU it is a stub. Parsed so the caller can compare against GTT and
        # know which one to trust; it was previously declared and never filled.
        elif "vram total memory" in low:
            res["vram_total"] = _first_int(line)
        elif "vram total used" in low:
            res["vram_used"] = _first_int(line)
    return res


def _first_int(line: str) -> int | None:
    """First integer on a rocm-smi line, or None. Values reach us as floats from
    _nums, and a malformed number must not raise out of a metrics read — that
    would take the whole status page down over one odd line of driver output."""
    nums = _nums(line)
    if not nums:
        return None
    try:
        return int(nums[0])
    except (ValueError, OverflowError):
        return None


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
