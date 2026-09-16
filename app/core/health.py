"""What this machine needs from its operator: versions, disk, pending work.

Phase F of PORTAL_PLAN §4.5. The inputs are the list in docs/UPDATES.md — the
things that were previously a runbook a human walked through by hand:

* apt pending updates, with the GPU/kernel ones called out (they couple the
  driver, ROCm, and every binary built against them) and the reboot flag
* ROCm installed vs the ROCm the router resolves at launch vs the ROCm ComfyUI's
  torch wheel was built for — three versions that legitimately differ, and one
  rule for when they must not (§"Version-coupling watch items")
* Vulkan SDK versions installed vs the one the deployed build used
* disk: the big trees, the disposable ones, and free space
* model store: index freshness, presets that lost their directory, model
  directories the router cannot see, broken links in the ``~/ai/bin`` farm

Two rules this module keeps:

**Read-only.** Nothing here installs, reboots, deletes or restarts. The pane
*says* what needs doing and links to the control that does it; the controls live
on the Status page and in Settings, behind their own confirms (§6).

**Every probe degrades alone.** A missing binary, an unreadable directory or an
offline version check produces ``{"error": ...}`` in its own section — never a
500, and never a blank page. Each probe is cached on its own TTL, because they
cost wildly different amounts (a stat vs ``apt -s upgrade`` vs a web fetch).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import paths, serviceenv, systemd

# apt is the only package manager on this box (Ubuntu 26.04). Kept as a name,
# not a path, so tests can put a stand-in earlier on PATH.
APT = "apt-get"
DPKG = "dpkg-query"

# Packages that couple the GPU stack: applying these is when a rebuild may be
# needed, so they are separated from ordinary pending updates.
GPU_PKG_RE = re.compile(
    r"amdgpu|amdrocm|mesa-amdgpu|^rocm|[-_]rocm[-_]|hipcc|hip-runtime|^hip-|hsa-runtime|rocblas|hipblas")
KERNEL_PKG_RE = re.compile(r"linux-image|linux-headers|linux-modules|linux-generic")

_TTL = 90           # ordinary probes
_LONG_TTL = 600     # apt simulation, du of a 600 GB tree
_NET_TTL = 24 * 3600    # "latest known upstream version"
_NET_FAIL_TTL = 3600    # ... and don't retry an offline check every page load
_cache: dict[str, tuple[float, object]] = {}


def reset_cache() -> None:
    """Drop every cached probe result (the page's refresh button)."""
    _cache.clear()


def _cached(key: str, fn, ttl: float = _TTL, refresh: bool = False):
    now = time.time()
    if not refresh:
        hit = _cache.get(key)
        if hit and (now - hit[0] < ttl):
            return hit[1]
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001 — one broken probe must not sink the page
        value = {"error": f"{type(e).__name__}: {e}"}
    _cache[key] = (now, value)
    return value


def _run(args: list[str], timeout: int = 20) -> str:
    """Run a read-only command; '' on any failure (missing binary, timeout)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    return (p.stdout or "") + (p.stderr or "")


def _age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _vers(text: str) -> tuple:
    """Sortable tuple out of a dotted version, for `max()` over version strings."""
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4]) or (0,)


def _series(text: str) -> str | None:
    """`major.minor` of a version-ish string: '7.13.0a2026' -> '7.13'."""
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


# --------------------------------------------------------------------------- apt

def apt(refresh: bool = False) -> dict:
    """Pending updates, with the GPU/kernel ones separated.

    ``apt-get -s upgrade`` is a simulation: it needs no root, installs nothing,
    and reads only the package lists that are already on disk. Whether those
    lists are *fresh* is reported separately (`lists_age_h`), because "nothing
    pending" from week-old lists means something different.
    """
    def probe() -> dict:
        if not shutil.which(APT):
            return {"available": False, "pending": None,
                    "error": f"{APT} not found — this is not a Debian/Ubuntu host?"}
        out = _run([APT, "-s", "-q", "upgrade"], timeout=90)
        pending = []
        for line in out.splitlines():
            m = re.match(r"Inst (\S+)(?: \[([^\]]*)\])? \((\S+)? ?([^)]*)\)", line)
            if not m:
                continue
            name, cur, new, suite = m.groups()
            pending.append({
                "name": name,
                "from": cur or "(new)",
                "to": new or "?",
                "suite": (suite or "").strip().split(" ")[0],
                "gpu": bool(GPU_PKG_RE.search(name)),
                "kernel": bool(KERNEL_PKG_RE.search(name)),
            })
        err = None
        if not pending and re.search(r"^E:", out, re.M):
            err = re.search(r"^E:(.*)$", out, re.M).group(1).strip()
        age = _age(apt_stamp())
        return {
            "available": True,
            "pending": len(pending),
            "packages": pending[:40],
            "gpu_pending": [p["name"] for p in pending if p["gpu"]],
            "kernel_pending": [p["name"] for p in pending if p["kernel"]],
            "lists_age_h": round(age / 3600, 1) if age is not None else None,
            "error": err,
        }

    return _cached("apt", probe, _LONG_TTL, refresh)


# ------------------------------------------------------------------------ ROCm

def apt_stamp() -> Path:
    """Where apt records its last successful `update`. Overridable for tests."""
    return Path(os.environ.get(
        "AI_PORTAL_APT_STAMP", "/var/lib/apt/periodic/update-success-stamp"))


def rocm_root() -> Path:
    """ROCm install root. Overridable so tests never read the real /opt/rocm."""
    return Path(os.environ.get("AI_PORTAL_ROCM_ROOT", "/opt/rocm"))


def _majority(versions) -> str | None:
    """The most common version — ROCm installs as many packages at once, and a
    half-applied upgrade shows up here as a disagreement worth seeing."""
    counts: dict[str, int] = {}
    for ver in versions:
        counts[ver] = counts.get(ver, 0) + 1
    return max(counts, key=lambda k: (counts[k], _vers(k))) if counts else None


def rocm_packages() -> dict[str, str]:
    """Installed ``amdrocm-*`` / ``rocm-core`` versions (base version, no epoch)."""
    pkgs: dict[str, str] = {}
    for line in _run([DPKG, "-W", "-f=${Package} ${Version}\n"], timeout=30).splitlines():
        name, _, ver = line.partition(" ")
        if re.match(r"^(amdrocm|rocm-core)", name or ""):
            base = re.match(r"[\d.]+", ver or "")
            if base:
                pkgs[name] = base.group(0)
    return pkgs


def installed_rocm() -> str | None:
    """The ROCm version the package manager says is installed — what a build
    compiled against. Recorded in the release registry at build time, because
    "which ROCm was this built for" stops being answerable once apt moves."""
    return _majority(rocm_packages().values())


def rocm(refresh: bool = False) -> dict:
    """ROCm as it is installed, as the services resolve it, and as HIP numbers it.

    The three spellings are all real and all matter:

    ``installed``  the ``amdrocm-*`` package version (what apt would update)
    ``hip``        ``hipcc --version`` (what a compiler-driven build sees)
    ``launch_dir`` the ``LD_LIBRARY_PATH`` entry in the shared service env —
                   *this* is the ROCm the router actually loads at runtime, and
                   a directory that no longer exists after an apt upgrade is a
                   real, findable failure
    """
    def probe() -> dict:
        root = rocm_root()
        cores = sorted((d.name for d in root.glob("core*") if d.is_dir()),
                       key=_vers, reverse=True) if root.is_dir() else []
        # dpkg gives the honest installed version; fall back to the core dir names
        pkgs = rocm_packages()
        version = _majority(pkgs.values())
        if not version and cores:
            version = re.sub(r"^core-?", "", cores[0]) or None
        hip = None
        for line in _run(["hipcc", "--version"], timeout=20).splitlines():
            m = re.search(r"HIP version:\s*([\d.]+)", line)
            if m:
                hip = m.group(1).rstrip(".")
                break
        launch = serviceenv.get_var("LD_LIBRARY_PATH") or ""
        rocm_paths = [p for p in launch.split(os.pathsep) if p and p.startswith(str(root))]
        launch_dir = rocm_paths[0] if rocm_paths else None
        return {
            "root": str(root),
            "root_exists": root.is_dir(),
            "installed": version,
            "hip": hip,
            "cores": cores[:6],
            "packages": len(pkgs),
            "launch_dir": launch_dir,
            "launch_dir_ok": bool(launch_dir) and Path(launch_dir).is_dir(),
            "launch_dirs_missing": [p for p in rocm_paths if not Path(p).is_dir()],
            "error": None if (root.is_dir() or pkgs) else f"no ROCm at {root}",
        }

    return _cached("rocm", probe, _TTL, refresh)


def torch(refresh: bool = False) -> dict:
    """ComfyUI's torch, read out of the venv's dist-info name.

    Never imports torch (that would load CUDA/ROCm libraries from a web request).
    The version string itself carries the ROCm it was built for:
    ``torch-2.12.0a0+rocm7.13.0a20260411``.
    """
    def probe() -> dict:
        site = paths.COMFY_VENV / "lib"
        dists = sorted(site.glob("python*/site-packages/torch-*.dist-info")) if site.is_dir() else []
        if not dists:
            return {"installed": False, "error": f"no torch dist-info under {paths.COMFY_VENV}"}
        name = dists[-1].name[len("torch-"):-len(".dist-info")]
        rocm_built = None
        m = re.search(r"rocm(\d+\.\d+(?:\.\d+)?)", name)
        if m:
            rocm_built = m.group(1)
        cfg = paths.COMFY_VENV / "pyvenv.cfg"
        py = None
        if cfg.is_file():
            m = re.search(r"^version\s*=\s*([\d.]+)", cfg.read_text(), re.M)
            py = m.group(1) if m else None
        return {"installed": True, "version": name, "rocm_built": rocm_built,
                "series": _series(rocm_built), "python": py,
                "age_h": round((_age(dists[-1]) or 0) / 3600, 1)}

    return _cached("torch", probe, _TTL, refresh)


def comfy_install(refresh: bool = False) -> dict:
    """ComfyUI's repo commit — the other half of "the venv moved or the repo
    did?" when a workflow stops working. Local ``git`` read, never a fetch."""
    def probe() -> dict:
        repo = paths.COMFY_REPO
        if not (repo / ".git").exists():
            return {"repo": str(repo), "commit": None,
                    "error": f"no git repo at {repo}"}
        out = _run(["git", "-C", str(repo), "log", "-1",
                    "--format=%h %cd", "--date=short"], timeout=15)
        parts = out.strip().split(" ", 1)
        return {"repo": str(repo),
                "commit": parts[0] or None,
                "committed": parts[1] if len(parts) > 1 else None}

    return _cached("comfy_install", probe, _TTL, refresh)


# --------------------------------------------------------------- Vulkan SDK

# UPDATES.md suggests scraping https://vulkan.lunarg.com/sdk/home for the newest
# SDK. That page is a JavaScript portal (LunarXchange) and its HTML contains no
# version at all — verified. The tags on KhronosGroup/Vulkan-Headers *are* the
# SDK versions (`vulkan-sdk-1.4.357.0`), are public, and need no auth; a 24 h
# cache keeps well inside GitHub's unauthenticated rate limit.
VULKAN_TAGS = ("https://api.github.com/repos/KhronosGroup/Vulkan-Headers/tags"
               "?per_page=30")
VULKAN_TAG_RE = re.compile(r"vulkan-sdk-(\d+\.\d+\.\d+\.\d+)")


def _fetch_latest_vulkan() -> dict:
    """Newest published Vulkan SDK version (cached a day; failures an hour)."""
    import httpx

    try:
        r = httpx.get(VULKAN_TAGS, timeout=6.0, follow_redirects=True,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        found = set(VULKAN_TAG_RE.findall(r.text))
        versions = sorted(found, key=_vers)
        return {"latest": versions[-1] if versions else None,
                "checked_at": time.time(), "source": "KhronosGroup/Vulkan-Headers tags"}
    except Exception as e:  # noqa: BLE001 — offline is normal here, not an error page
        return {"latest": None, "checked_at": time.time(),
                "error": f"{type(e).__name__}"}


def vulkan(refresh: bool = False) -> dict:
    """SDKs installed under ~/ai/sdk/vulkan, the one the deployed build used."""
    def probe() -> dict:
        root = paths.AI_ROOT / "sdk" / "vulkan"
        installed = sorted((d.name for d in root.iterdir() if d.is_dir()),
                           key=_vers, reverse=True) if root.is_dir() else []
        used = None
        try:
            import json
            reg = json.loads(paths.RELEASES_JSON.read_text())
            entries = {r.get("name"): r for r in reg.get("releases", [])}
            cur = entries.get(reg.get("current")) or {}
            used = cur.get("vulkan_sdk")
        except Exception:  # noqa: BLE001 — registry absent or half-written
            pass
        # a failed lookup is remembered for an hour, so an offline box does not
        # pay 5 s on every page load
        prev = _cache.get("vulkan_latest")
        ttl = _NET_TTL
        if prev and prev[1].get("error"):
            ttl = _NET_FAIL_TTL
        latest = _cached("vulkan_latest", _fetch_latest_vulkan, ttl, refresh)
        return {
            "installed": installed,
            "newest": installed[0] if installed else None,
            "used_by_current_build": used,
            "latest_known": latest.get("latest"),
            "checked_at": latest.get("checked_at"),
            "check_error": latest.get("error"),
        }

    return _cached("vulkan", probe, _TTL, refresh)


# ---------------------------------------------------------------------- disk

#: (label, path, note). The big trees are what fills the disk; the disposable
#: ones are what frees it, which is why they are listed next to each other.
DISK_TARGETS = (
    ("AI tree", paths.AI_ROOT, ""),
    ("Models (llama.cpp)", paths.LLAMA_MODELS, "router --models-dir"),
    ("ComfyUI (incl. venv)", paths.COMFY_REPO, ""),
    ("Hugging Face cache", paths.HF_CACHE, "kept here on purpose (F11)"),
    ("Stored builds", paths.RELEASES, "deletable from the Status page"),
    ("cmake build trees", paths.BUILD_ROOT, "disposable between builds"),
    ("Rollback notes", paths.ROLLBACK_DIR, ""),
)


def du_bytes(path: Path) -> int | None:
    """`du -sxb`: apparent bytes, one filesystem. ~0.5s over 600 GB of models."""
    out = _run(["du", "-sxb", str(path)], timeout=120)
    m = re.match(r"(\d+)\s", out)
    return int(m.group(1)) if m else None


def disk(refresh: bool = False) -> dict:
    def probe() -> dict:
        rows = []
        for label, path, note in DISK_TARGETS:
            if not path.exists():
                rows.append({"label": label, "path": str(path), "bytes": None,
                             "note": note or "not present"})
                continue
            rows.append({"label": label, "path": str(path), "bytes": du_bytes(path),
                         "note": note})
        free = shutil.disk_usage(paths.AI_ROOT if paths.AI_ROOT.exists() else Path("/"))
        return {
            "trees": rows,
            "free_bytes": free.free,
            "total_bytes": free.total,
            "used_pct": round(100.0 * (free.total - free.free) / free.total, 1) if free.total else None,
        }

    return _cached("disk", probe, _LONG_TTL, refresh)


# ------------------------------------------------------------- model store

def model_store(refresh: bool = False) -> dict:
    """Freshness of the gguf-indexer index, and what the router can actually see.

    The layout rules (docs/UPDATES.md, code-verified against
    ``common/preset.cpp``) are what make the two checks here meaningful: the
    router scans ``--models-dir`` **one level deep**, so a model parked in a
    sub-subfolder exists but is invisible, and a preset section whose directory
    is gone is a lie the router will try to honour.
    """
    def probe() -> dict:
        index_path = paths.LLAMA_MODELS / "INDEX.json"
        index = {}
        if index_path.is_file():
            import json
            try:
                raw = json.loads(index_path.read_text())
                scan = raw.get("scan") or {}
                index = {
                    "exists": True,
                    "age_h": round((_age(index_path) or 0) / 3600, 1),
                    "version": raw.get("index_version"),
                    "files_on_disk": scan.get("files_on_disk"),
                    "total_bytes": scan.get("total_bytes"),
                }
            except Exception as e:  # noqa: BLE001
                index = {"exists": True, "error": f"unreadable: {e}"}
        else:
            index = {"exists": False}

        presets = []
        if paths.PRESETS_INI.is_file():
            presets = re.findall(r"^\[([^\]]+)\]", paths.PRESETS_INI.read_text(), re.M)
        sections = {s for s in presets if s not in ("*",)}

        on_disk = []
        hidden: list[str] = []
        empty_dirs: list[str] = []
        if paths.LLAMA_MODELS.is_dir():
            for d in sorted(p for p in paths.LLAMA_MODELS.iterdir() if p.is_dir()):
                files = [f for f in d.iterdir() if f.is_file()]
                ggufs = [f for f in files if f.suffix.lower() == ".gguf"]
                if not ggufs:
                    empty_dirs.append(d.name)
                on_disk.append(d.name)
                # sub-subdirectories are never scanned: a model in there is invisible
                for sub in d.iterdir():
                    if sub.is_dir() and any(f.suffix.lower() in (".gguf", ".safetensors")
                                            for f in sub.iterdir() if f.is_file()):
                        hidden.append(f"{d.name}/{sub.name}")
        stale_presets = sorted(sections - set(on_disk))

        broken: list[str] = []
        farm = paths.AI_BIN
        if farm.is_dir():
            broken = sorted(l.name for l in farm.iterdir()
                            if l.is_symlink() and not l.exists())

        return {
            "index": index,
            "presets": {"exists": paths.PRESETS_INI.is_file(),
                        "age_h": round((_age(paths.PRESETS_INI) or 0) / 3600, 1),
                        "sections": len(sections)},
            "model_dirs": len(on_disk),
            "stale_presets": stale_presets[:10],
            "invisible_models": hidden[:10],
            "dirs_without_model": empty_dirs[:10],
            "broken_links": broken[:10],
        }

    return _cached("model_store", probe, _TTL, refresh)


# ------------------------------------------------------------------ advisories

# The GTT aperture this box is configured for, and the amdgpu default it was
# moved off. /etc/modprobe.d/amdttm.conf sets pages_limit=28311552 (108 GiB);
# the driver default caps the aperture at 64 GiB, which is below the size of the
# 88 GiB model this machine serves. See PORTAL_PLAN.md §6.12.0.
EXPECTED_GTT_GIB = 108.0
AMDGPU_DEFAULT_GTT_GIB = 64.0


def aperture_advisory(gpu: dict | None) -> list[dict]:
    """Warn when the GPU's view of memory is smaller than it was configured to be.

    The 64 GiB ceiling is dangerous precisely because it fails quietly: a model
    larger than the aperture still *loads* and then reports a 4096-token context,
    which looks like a bad model rather than lost configuration. A driver update
    or a dropped /etc/modprobe.d/amdttm.conf returns the box to that state with
    nothing visible wrong until a model starts lying about its context.

    Pure function of the dict it is given, so it is testable without a GPU.
    """
    out: list[dict] = []
    # `None` means "not probed" and stays quiet; a dict with no gtt_total means
    # "probed and could not read the aperture", which is the case worth saying.
    # They must not collapse: the first is normal on a machine without amdgpu,
    # the second is this box losing its configuration.
    if gpu is None:
        return out
    total = gpu.get("gtt_total") or 0
    if not total:
        # No amdgpu aperture readable at all: on this machine that means the
        # driver is missing or the sysfs node moved, not that memory is fine.
        out.append({"level": "info",
                    "text": "GPU memory aperture (GTT) could not be read, so the "
                            "memory budget cannot be checked against it. On an iGPU "
                            "rocm-smi's VRAM figure is a ~512 MiB stub and is not "
                            "the pool models load into.",
                    "where": "/health", "where_label": "Health", "cmd": ""})
        return out
    gib = total / (1 << 30)
    if gib < AMDGPU_DEFAULT_GTT_GIB + 1:
        out.append({"level": "bad",
                    "text": f"GPU aperture is only {gib:.0f} GiB — the amdgpu "
                            f"default. A model larger than this still loads and then "
                            f"reports a 4096-token context, so it fails as a “bad "
                            f"model” rather than as missing configuration. "
                            f"This box needs ~{EXPECTED_GTT_GIB:.0f} GiB via "
                            "/etc/modprobe.d/amdttm.conf.",
                    "where": "/health", "where_label": "Health",
                    "cmd": "cat /etc/modprobe.d/amdttm.conf"})
    elif gib < EXPECTED_GTT_GIB - 1:
        out.append({"level": "warn",
                    "text": f"GPU aperture is {gib:.0f} GiB, below the {EXPECTED_GTT_GIB:.0f} "
                            f"GiB this machine is configured for "
                            f"(/etc/modprobe.d/amdttm.conf pages_limit). Check for a "
                            f"dropped config or a driver update that reset it.",
                    "where": "/health", "where_label": "Health",
                    "cmd": "cat /etc/modprobe.d/amdttm.conf"})
    return out


def advisories(release: dict | None = None, service: dict | None = None,
               comfy: dict | None = None, builds: dict | None = None,
               aperture: dict | None = None) -> list[dict]:
    """What the pane puts first: the things that need a decision.

    ``release`` (the deployed llama.cpp release record) and the two unit states
    are passed in rather than probed here, so this stays a pure function of what
    the caller already has — and so it can be tested without a machine.
    """
    out: list[dict] = []

    def add(level: str, text: str, where: str = "", label: str = "",
            cmd: str = "") -> None:
        out.append({"level": level, "text": text, "where": where,
                    "where_label": label, "cmd": cmd})

    # First, because a shrunken aperture invalidates every capacity judgement the
    # rest of the pane makes.
    out.extend(aperture_advisory(aperture))

    rp = systemd.reboot_pending()
    if rp.get("reboot_required"):
        add("warn", "A reboot is pending: the running kernel/driver is older than "
                    "what is installed. Every GPU binary is using the old one.",
            cmd="reboot")

    a = apt()
    if a.get("error"):
        add("warn", f"apt pending-updates check failed: {a['error']}")
    elif a.get("available"):
        gpu_pkgs = a.get("gpu_pending") or []
        if gpu_pkgs:
            add("warn", f"{len(gpu_pkgs)} GPU-stack package(s) pending: "
                        f"{', '.join(gpu_pkgs[:5])}. "
                        "The portal never applies apt updates. After applying them, "
                        "rebuild llama.cpp if the ROCm series moved.",
                where="/#update-llamacpp", label="llama.cpp update pane")
        if a.get("pending"):
            add("info", f"{a['pending']} package update(s) pending "
                        f"(apt; lists {a.get('lists_age_h')}h old)" if a.get("lists_age_h") is not None
                        else f"{a['pending']} package update(s) pending")

    r = rocm()
    if r.get("error"):
        add("warn", f"ROCm: {r['error']}")
    if r.get("launch_dirs_missing"):
        add("bad", "The service environment's LD_LIBRARY_PATH points at ROCm "
                   "directories that no longer exist: "
                   f"{', '.join(r['launch_dirs_missing'])}. The router will fail to "
                   "start until it is repointed.",
            where="/settings", label="Settings")

    t = torch()
    if t.get("installed") and t.get("series") and r.get("installed"):
        if t["series"] != _series(r["installed"]):
            add("info",
                f"ComfyUI's torch was built for ROCm {t['rocm_built']} while the system "
                f"runs ROCm {r['installed']}. torch ships its own ROCm libraries, so this "
                "works and is not urgent; aligning them means reinstalling torch "
                "(several GB).",
                where="/#update-comfyui", label="ComfyUI update pane")

    v = vulkan()
    if v.get("latest_known") and v.get("newest") and \
            _vers(v["latest_known"]) > _vers(v["newest"]):
        add("info", f"Vulkan SDK {v['latest_known']} is out and {v['newest']} is installed. "
                    "This only matters for a fresh build directory — glslc is pinned into "
                    "the CMake cache at configure time — so it is a note for the next build.",
            where="/#update-llamacpp", label="llama.cpp update pane")
    if v.get("newest") and v.get("used_by_current_build"):
        used = str(v["used_by_current_build"]).rstrip("/").split("/")[-1]
        if _vers(used) < _vers(v["newest"]):
            add("info", f"The deployed llama.cpp was built with Vulkan SDK {used}, but "
                        f"{v['newest']} is installed. Not a problem until you rebuild — "
                        "a newer glslc can produce better shader code.",
                where="/#update-llamacpp", label="llama.cpp update pane")
    if release is not None and not release.get("rocm"):
        add("info", "The deployed build does not record which ROCm it was compiled "
                    "against, so version drift can only be checked from the runtime "
                    "libraries. New builds record it.")

    # Build directories the registry never recorded. Passed in like the rest,
    # because finding them means walking ~/ai/releases and /proc — and because
    # health.py must not import a provider to ask.
    b = builds or {}
    strays = b.get("strays") or []
    if strays:
        gib = (b.get("bytes") or 0) / 1024 ** 3
        names = ", ".join(s["name"] for s in strays[:3])
        ahead = [s for s in strays if s.get("ahead")]
        if ahead:
            add("warn", f"An update left {ahead[0]['name']} built on disk and never "
                        f"deployed it — it is newer than the running build and is "
                        f"not in the registry, so it appears in no build list. "
                        f"{len(strays)} such director{'y' if len(strays) == 1 else 'ies'}"
                        f", {gib:.1f} GiB: {names}.",
                where="/#update-llamacpp", label="Builds panel")
        else:
            add("warn", f"{len(strays)} build director{'y' if len(strays) == 1 else 'ies'} "
                        f"on disk that the registry never recorded — {gib:.1f} GiB "
                        f"({names}). Nothing lists them, so nothing offers to remove "
                        f"them; purge from the Builds panel.",
                where="/#update-llamacpp", label="Builds panel")
    for u in (b.get("unexpected") or [])[:1]:
        add("info", f"{u['name']} sits in ~/ai/releases but is not a build the portal "
                    "names (expected llama.cpp-b<N>), so it is reported and never "
                    "offered for deletion.",
            where="/#update-llamacpp", label="Builds panel")

    d = disk()
    if d.get("free_bytes") is not None and d["free_bytes"] < 50 * 1024 ** 3:
        add("warn", f"Only {_human_gb(d['free_bytes'])} free on the AI filesystem. "
                    "Stored builds and cmake build trees are the usual place to start.",
            where="/#update-llamacpp", label="llama.cpp update pane")

    m = model_store()
    idx = m.get("index") or {}
    if not idx.get("exists"):
        add("warn", "No model index (INDEX.json) — the gguf-indexer has never run here.")
    elif idx.get("age_h") is not None and idx["age_h"] > 24 * 7:
        add("warn", f"Model index is {idx['age_h'] / 24:.0f} days old: model listings "
                    "and hashes may not match what is on disk.")
    if m.get("broken_links"):
        add("bad", f"Broken links in the bin farm: {', '.join(m['broken_links'][:5])} — "
                   "those commands will fail until they are relinked.",
            where="/#update-llamacpp", label="llama.cpp update pane")
    if m.get("invisible_models"):
        add("warn", f"{len(m['invisible_models'])} model folder(s) hold model files in a "
                    f"sub-subdirectory ({', '.join(m['invisible_models'][:3])}) — llama.cpp "
                    "scans one level deep, so it cannot see them.")
    if m.get("stale_presets"):
        add("warn", f"{len(m['stale_presets'])} preset section(s) have no model directory: "
                    f"{', '.join(m['stale_presets'][:3])}")

    for name, unit in (("router", service or {}), ("ComfyUI", comfy or {})):
        state = unit.get("state") or unit.get("ActiveState")
        if state and state not in ("active", "unknown"):
            add("warn", f"{name} service is {state}.", where="/", label="Status page")

    order = {"bad": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: order.get(a["level"], 9))
    return out


def host() -> dict:
    """Which machine this is — the pane is read over SSH, so say so."""
    import platform
    import socket

    pretty = None
    osr = Path("/etc/os-release")
    if osr.is_file():
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)', osr.read_text(), re.M)
        pretty = m.group(1) if m else None
    uptime = None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return {"hostname": socket.gethostname(), "os": pretty or platform.system(),
            "kernel": platform.release(), "uptime_s": uptime,
            "python": platform.python_version()}


def attention_count(refresh: bool = False) -> int:
    """How many things a human should look at — one number for the dashboard."""
    apt(); rocm(); torch(); vulkan(); disk(); model_store()   # warm the cache
    return sum(1 for a in advisories() if a["level"] in ("bad", "warn"))


def _human_gb(n: int) -> str:
    return f"{n / 1024 ** 3:.0f} GB"


def gpu_aperture(refresh: bool = False) -> dict | None:
    """The GPU's memory aperture, for the budget check and the health page.

    Wrapped so a missing driver, an unreadable sysfs node, or a crash in the
    metrics layer degrades to None instead of taking the whole health page down.
    None means "not read", distinct from a dict with no gtt_total, and
    aperture_advisory relies on that distinction.

    This is the figure the §4.7 budget is planned against. It is *not* what
    rocm-smi reports as VRAM: on an iGPU that is a ~512 MiB stub while GTT is
    ~108 GiB (§6.12.0).
    """
    try:
        from .. import metrics
    except Exception:  # noqa: BLE001 - metrics is optional to the health pane
        return None
    try:
        # gpu() already honours its own ~5 s cache, so the health page polling it
        # costs nothing; _GPU_CACHE is only consulted to avoid a second call when
        # the entry is known-fresh.
        cache = getattr(metrics, "_GPU_CACHE", (0.0, None))
        if not refresh and cache[1] is not None and \
                time.time() - cache[0] < getattr(metrics, "_GPU_TTL", 5):
            return cache[1]
        return metrics.gpu()
    except Exception:  # noqa: BLE001
        return None


def summary(release: dict | None = None, service: dict | None = None,
            comfy: dict | None = None, builds: dict | None = None,
            refresh: bool = False) -> dict:
    """Everything, for /api/health and the health page."""
    aperture = gpu_aperture(refresh)
    body = {
        "generated_at": time.time(),
        "apt": apt(refresh),
        "rocm": rocm(refresh),
        "torch": torch(refresh),
        "comfyui": comfy_install(refresh),
        "vulkan": vulkan(refresh),
        "disk": disk(refresh),
        "model_store": model_store(refresh),
        "gpu": aperture,
    }
    body["advisories"] = advisories(release=release, service=service, comfy=comfy,
                                    builds=builds, aperture=aperture)
    return body
