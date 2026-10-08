"""ComfyUI provider.

ComfyUI is a git repo + a Python venv together at ~/ai/comfyui (finding F8).
Updates are git + venv sync — there is NO compile step (F8).

Phase E adds the service layer: a static ``ai-comfyui`` user unit whose launch
parameters come from a portal-generated env file (see core.units), plus
the model inventory and portal-side downloads that place files into the right
``models/<category>/`` folder — no browser downloads, no manual copying.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import Provider, ToolStatus
from ..core import db, health, paths, serviceconf, units, systemd, updater, modelmeta

# Launch parameters, stored at ~/ai/config/comfyui.json. The env file the
# service reads is *generated* from this; changing the json (via the API or by
# hand) + restart is the whole reconfiguration story.
DEFAULT_CONFIG = {"listen": "127.0.0.1", "port": 8188, "extra_args": ""}
CONFIG_FILE = paths.CONFIG / "comfyui.json"

COMFY_WRAPPER = """#!/bin/sh
# AI portal - ComfyUI launch wrapper (GENERATED, do not hand-edit).
# All parameters come from ~/ai/config/comfyui.env; change them via the
# portal's Settings page, then restart ai-comfyui.
set -eu
if [ ! -x "${COMFY_PYTHON:-}" ]; then
  echo "ai-comfyui: no python at ${COMFY_PYTHON:-<unset>}" >&2
  exit 1
fi
exec "$COMFY_PYTHON" "$COMFY_MAIN" --listen "$COMFY_HOST" --port "$COMFY_PORT" $COMFY_ARGS
"""


def _venv_info() -> dict:
    """What the venv is, without importing torch.

    ``import torch`` costs ~1.3 s, and this runs inside ``status()`` — which the
    dashboard calls on every provider, every refresh. The version is readable
    from the dist-info directory name, which is the same fact pip installed.
    """
    info: dict = {}
    python, source = service_python()
    info["python_bin"], info["python_src"] = python, source
    cfg = paths.COMFY_VENV / "pyvenv.cfg"
    if cfg.is_file():
        m = re.search(r"^version\s*=\s*([\d.]+)", cfg.read_text(), re.M)
        info["python"] = m.group(1) if m else None
    site = paths.COMFY_VENV / "lib"
    dists = sorted(site.glob("python*/site-packages/torch-*.dist-info")) \
        if site.is_dir() else []
    if dists:
        info["torch"] = dists[-1].name[len("torch-"):-len(".dist-info")]
        info["is_rocm_build"] = "+rocm" in (info.get("torch") or "")
    else:
        info["torch"] = None
    if not paths.COMFY_VENV.is_dir():
        info["error"] = "venv missing"
    return info


#: Packages on this machine built against ROCm, which do not exist on PyPI
#: (e.g. ``torch 2.12.0a0+rocm7.13.0a20260411``). ComfyUI's own requirements.txt
#: lists ``torch``, ``torchvision``, ``torchaudio`` **unpinned**: a plain
#: ``pip install -r requirements.txt`` leaves an installed ROCm build alone, but
#: ComfyUI's documented *upgrade* command would pull the CUDA wheels off PyPI,
#: drop ~3 GB of nvidia-* in, and leave ComfyUI with no GPU. So these are pinned
#: in a generated constraints file: everything else may move, these do not.
PROTECTED_PACKAGES = ("torch", "torchvision", "torchaudio", "torchdata",
                      "torchsde", "triton", "pytorch-triton",
                      "pytorch-triton-rocm")

#: What the post-update check imports. If this fails the venv is broken and the
#: service must NOT be restarted into it.
VERIFY_IMPORT = (
    "import json,torch;"
    "print(json.dumps({'torch': torch.__version__, 'hip': torch.version.hip, "
    "'cuda': torch.version.cuda}))")


def _read_env_pairs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def service_python() -> tuple[str, str]:
    """The interpreter the **ai-comfyui service** runs, read from its launch env.

    Updating some other venv is how an update looks successful and changes
    nothing, so the updater installs into this one and says where it came from.
    Falls back to the repo's own ``venv/`` when the env file has no
    ``COMFY_PYTHON`` (a hand-built install), and says so in the log.
    """
    env = _read_env_pairs(paths.CONFIG / "comfyui.env")
    configured = env.get("COMFY_PYTHON", "").strip()
    if configured and Path(configured).is_file():
        return configured, "COMFY_PYTHON from ~/ai/config/comfyui.env (the service's own)"
    fallback = paths.COMFY_VENV / "bin" / "python"
    return str(fallback), (
        "~/ai/comfyui/venv (default; "
        + (f"COMFY_PYTHON={configured!r} does not exist" if configured
           else "COMFY_PYTHON not set") + ")")


def installed_versions(python: str, names: tuple[str, ...]) -> dict[str, str]:
    """Installed versions of ``names`` in that venv, via one pip call."""
    if not names:
        return {}
    args = [python, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"]
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=120).stdout
        rows = json.loads(out or "[]")
    except Exception:  # noqa: BLE001 — a version report is never fatal
        return {}
    want = {n.lower().replace("_", "-") for n in names}
    return {r["name"].lower().replace("_", "-"): r["version"]
            for r in rows
            if r.get("name", "").lower().replace("_", "-") in want}


def requirements_names(requirements: Path) -> list[str]:
    """The distribution names a requirements file mentions (no versions)."""
    names: list[str] = []
    if not requirements.is_file():
        return names
    for line in requirements.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        names.append(re.split(r"[=<>!~;\[\s]", line, maxsplit=1)[0]
                     .lower().replace("_", "-"))
    return names


def write_constraints(python: str, requirements: Path, log,
                      dest: Path) -> tuple[Path | None, dict[str, str]]:
    """Pin this venv's local builds (``+rocm…``) plus the torch family.

    Two sources, because each alone misses a case:

    * every installed package whose version carries a local segment (``+rocm``,
      ``+git``) — nothing on an index can satisfy those, and they are exactly the
      ones pip would happily replace; and
    * the names in ``PROTECTED_PACKAGES`` that ``requirements.txt`` mentions and
      that are installed here — so a missing torch stays installable rather than
      being pinned to a version that does not exist anywhere.

    Returns ``(path, pins)``; ``(None, {})`` when there is nothing to hold.
    """
    listed = set(requirements_names(requirements))
    pins: dict[str, str] = {}
    try:
        rows = json.loads(subprocess.run(
            [python, "-m", "pip", "list", "--format=json",
             "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=120).stdout or "[]")
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        name = r.get("name", "").lower().replace("_", "-")
        ver = r.get("version", "")
        if not name or not ver:
            continue
        # a local segment (+rocm, +git) means nothing on an index can satisfy it,
        # and it is exactly what pip would otherwise replace; the torch family is
        # held even at a plain version, because requirements.txt lists it bare
        if name in PROTECTED_PACKAGES or ("+" in ver and name in listed):
            pins[name] = ver
    if not pins:
        return None, {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(f"{n}=={v}" for n, v in sorted(pins.items())) + "\n")
    log(f"holding {len(pins)} machine-specific package(s) "
        f"(this box's builds, not on PyPI): "
        + ", ".join(f"{n}=={v}" for n, v in sorted(pins.items())[:6])
        + (" …" if len(pins) > 6 else ""))
    return dest, pins


def verify_python(python: str, log) -> dict:
    """Import torch in the target venv before restarting the service into it.

    A dependency sync that pulls the wrong torch is exactly the failure that
    looks fine in the pip output and breaks the service on next start — so check
    it while the old service is still running and the venv can be restored.
    """
    try:
        p = subprocess.run([python, "-c", VERIFY_IMPORT],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if p.returncode != 0:
        return {"ok": False,
                "error": ((p.stderr or p.stdout).strip().splitlines() or [""])[-1][:300]}
    try:
        info = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "torch printed no version"}
    system_rocm = bool(health.installed_rocm())
    if system_rocm and not info.get("hip"):
        return {"ok": False,
                "error": f"torch {info.get('torch')} has no ROCm support "
                         "(this machine runs ROCm " + str(system_rocm) + ") — "
                         "a CUDA build was installed into the service's venv"}
    log(f"verified: torch {info.get('torch')} "
        f"({'ROCm ' + str(info['hip']) if info.get('hip') else 'no ROCm'})")
    return {"ok": True, **info}


def _git(repo: str | Path, *args: str) -> str | None:
    """git in `repo`, or None if it failed. Accepts the Path the paths table stores."""
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
    def env_mapping(cfg: dict) -> dict[str, str]:
        """Launch parameters as discrete env vars — host/port are separate so
        the Settings page can manage them via serviceconf; only the free-form
        extra args stay in COMFY_ARGS (word-split by the wrapper)."""
        return {
            "COMFY_PYTHON": str(paths.COMFY_VENV / "bin" / "python"),
            "COMFY_MAIN": str(paths.COMFY_REPO / "main.py"),
            "COMFY_HOST": cfg["listen"],
            "COMFY_PORT": str(cfg["port"]),
            "COMFY_ARGS": cfg.get("extra_args", ""),
        }

    def set_config(self, cfg: dict) -> dict:
        """Persist validated config and regenerate the service env file."""
        cfg = self.validate_config(cfg)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
        units.write_env(paths.COMFY_UNIT_NAME, self.env_mapping(cfg),
                             log=lambda m: None)
        return cfg

    @staticmethod
    def wrapper_text() -> str:
        """The generated ComfyUI wrapper.

        ``$COMFY_ARGS`` is deliberately unquoted: the wrapper's shell splits it
        into real argv words, which is the whole reason a wrapper exists here
        instead of an ExecStart line.
        """
        return COMFY_WRAPPER

    def ensure_service(self, log=print) -> dict:
        """Generate env file + wrapper + static unit (all idempotent).

        Does NOT start the service — the portal's service controls do that.
        """
        cfg = self.validate_config(self.config())
        units.write_env(paths.COMFY_UNIT_NAME, self.env_mapping(cfg), log)
        units.write_wrapper(paths.COMFY_UNIT_NAME, self.wrapper_text(), log)
        units.install_unit(paths.COMFY_UNIT, paths.COMFY_UNIT_NAME,
                                "ComfyUI (AI Portal)", str(paths.COMFY_REPO), log)
        return {
            "unit": paths.COMFY_UNIT,
            "env": str(units.env_path(paths.COMFY_UNIT_NAME)),
            "wrapper": str(units.wrapper_path(paths.COMFY_UNIT_NAME)),
            "config": cfg,
        }

    # --------------------------------------------------- model inventory
    @staticmethod
    def categories() -> list[str]:
        """Model subfolders ComfyUI searches — excluding configs/ (per-arch
        config jsons, not models) and hidden dirs."""
        if not paths.COMFY_MODELS.is_dir():
            return []
        return sorted(d.name for d in paths.COMFY_MODELS.iterdir()
                      if d.is_dir() and not d.name.startswith((".", "_"))
                      and d.name not in modelmeta.NON_MODEL_CATEGORIES)

    def inventory(self) -> list[dict]:
        """models/<category>/<file> — per-category count, size, and files.

        Only .safetensors are listed: this library is safetensors-based, and
        the folders also hold ComfyUI's put_*_here flag files (no extension).
        """
        out = []
        for cat in self.categories():
            d = paths.COMFY_MODELS / cat
            files = [{"name": f.name, "size": f.stat().st_size,
                      "mtime": f.stat().st_mtime}
                     for f in sorted(d.iterdir())
                     if f.is_file() and f.suffix == ".safetensors"]
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

    # ------------------------------------------- update pipeline (no build)
    def remote_state(self, force: bool = False) -> dict:
        """How far the checkout is behind origin/master (TTL-cached, same
        pattern as the llama.cpp probe; stale-on-offline by design)."""
        cached = db.get_setting("comfyui_remote_state")
        ttl = float(db.get_setting("remote_check_ttl_hours", 6.0) or 6.0)
        if (not force and isinstance(cached, dict)
                and time.time() - cached.get("checked_at", 0) < ttl * 3600):
            return cached
        repo = paths.COMFY_REPO
        if not (repo / ".git").exists():
            return {"error": "comfyui repo missing"}
        try:
            subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"],
                           capture_output=True, text=True, timeout=120)
        except Exception:  # noqa: BLE001
            st = dict(cached) if isinstance(cached, dict) else {}
            st.update({"stale": True, "error": "fetch failed (offline?)"})
            return st

        def head(ref: str) -> str | None:
            out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                                  f"{ref}^{{commit}}"],
                                 capture_output=True, text=True,
                                 timeout=10).stdout.strip()
            return out or None

        local, remote = head("HEAD"), head("origin/master")
        behind = None
        if local and remote:
            out = subprocess.run(["git", "-C", str(repo), "rev-list", "--count",
                                  f"{local}..{remote}"],
                                 capture_output=True, text=True,
                                 timeout=10).stdout.strip()
            behind = int(out) if out.isdigit() else None
        st = {"checked_at": time.time(), "local_head": local,
              "remote_head": remote, "behind": behind,
              "update_available": bool(behind and behind > 0)}
        db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("comfyui_remote_state", json.dumps(st)))
        return st

    def update_status(self) -> dict:
        """Everything the dashboard's ComfyUI update panel needs."""
        return {
            "commit": _git(paths.COMFY_REPO, "rev-parse", "HEAD"),
            "repo": str(paths.COMFY_REPO),
            "remote": self.remote_state(),
            "recent": self._recent_commits(8),
            "venv": _venv_info(),
            "job": updater.latest_job("comfyui"),
        }

    @staticmethod
    def _recent_commits(n: int = 8) -> list[dict]:
        r = subprocess.run(["git", "-C", str(paths.COMFY_REPO), "log",
                            f"--max-count={n}", "--format=%h|%s|%ci"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        out = []
        for ln in r.stdout.splitlines():
            parts = ln.split("|", 2)
            if len(parts) == 3:
                out.append({"sha": parts[0], "subject": parts[1],
                            "date": parts[2][:10]})
        return out

    def update(self, restart: bool = True, upgrade: bool = False,
               log=print) -> dict:
        """ComfyUI's documented update, done against the service's own venv.

        ComfyUI's README says: update the checkout, then ``pip install -r
        requirements.txt`` from inside it. That is what this does, with three
        things a hand-run version of it does not protect against:

        * **the venv is the one the service runs** — read from ``COMFY_PYTHON``
          in the launch env, not assumed from the repo layout (:func:`service_python`);
        * **this machine's ROCm builds are held in place** by a generated
          constraints file (:func:`write_constraints`). ``requirements.txt`` lists
          ``torch`` unpinned, and PyPI's torch is a CUDA build; ``upgrade=True``
          without this would take the GPU away;
        * **the venv is verified before the service is restarted into it**
          (:func:`verify_python`), while the old process is still up and the
          damage is a log line rather than a dead service.

        ``upgrade=False`` (default) satisfies requirements — ComfyUI's literal
        instruction. ``upgrade=True`` adds pip's ``--upgrade`` for the packages
        requirements.txt names, still under the holds.

        Rollback caveat, recorded in the job log: git can be reset back; pip
        packages installed by the update are not uninstalled by a rollback.
        """
        repo = paths.COMFY_REPO
        if not (repo / ".git").exists():
            raise RuntimeError("comfyui repo missing")
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True,
                               timeout=15).stdout.strip()
        if dirty:
            raise RuntimeError(
                "comfyui repo has local changes — not updating.\n"
                "Resolve them (commit, stash, or discard) and try again:\n"
                + "\n".join(dirty.splitlines()[:10]))
        python, python_src = service_python()
        log(f"venv python: {python}\n           ({python_src})")
        reqs = repo / "requirements.txt"
        before = installed_versions(python, tuple(requirements_names(reqs)))
        prev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              timeout=10).stdout.strip()
        log(f"current commit {prev[:12]}")
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"],
                       capture_output=True, text=True, timeout=120)
        behind = subprocess.run(["git", "-C", str(repo), "rev-list", "--count",
                                 f"{prev}..origin/master"],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip()
        if behind == "0":
            log("no upstream changes — requirements.txt unchanged, nothing to do")
            return {"status": "noop", "commit": prev, "python": python}
        log(f"updating: {behind} commit(s) behind origin/master")
        r = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only",
                            "--quiet", "origin", "master"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"git pull failed:\n{(r.stderr or r.stdout).strip()[:500]}")
        new = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
        log(f"pulled {prev[:12]} -> {new[:12]}")

        venv_pip = [python, "-m", "pip"]
        holds, pins = write_constraints(python, reqs, log,
                                       paths.PORTAL_STATE / "comfyui-constraints.txt")
        cmd = venv_pip + ["install", "-q"] + (["--upgrade"] if upgrade else []) \
            + (["-c", str(holds)] if holds else []) + ["-r", str(reqs)]
        log("syncing venv: " + " ".join(cmd[cmd.index("pip") + 1:]))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError("requirements sync failed:\n"
                               + (r.stderr or r.stdout)[-800:])
        mgr = repo / "manager_requirements.txt"
        if mgr.exists():
            cmd = venv_pip + ["install", "-q"] + (["--upgrade"] if upgrade else []) \
                + (["-c", str(holds)] if holds else []) + ["-r", str(mgr)]
            log("syncing venv: " + " ".join(cmd[cmd.index("pip") + 1:]))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            if r.returncode != 0:
                raise RuntimeError("manager requirements sync failed:\n"
                                   + (r.stderr or r.stdout)[-800:])

        after = installed_versions(python, tuple(requirements_names(reqs)))
        changed = {n: (before.get(n) or "(absent)", v) for n, v in after.items()
                   if before.get(n) != v}
        missing = sorted(set(before) - set(after))
        if changed:
            log(f"{len(changed)} package(s) changed: " + ", ".join(
                f"{n} {a} -> {b}" for n, (a, b) in sorted(changed.items())[:12])
                + (" …" if len(changed) > 12 else ""))
        else:
            log("no package versions changed (requirements already satisfied)")
        if missing:
            log(f"note: {len(missing)} listed package(s) not reported after sync: "
                + ", ".join(missing[:8]))
        for held, version in sorted(pins.items()):
            if after.get(held) and after[held] != version:
                raise RuntimeError(
                    f"{held} was replaced during the sync ({version} -> "
                    f"{after[held]}): this machine's build is gone. Restore it "
                    "from the ROCm wheel you installed before restarting ComfyUI.")

        check = verify_python(python, log)
        if not check.get("ok"):
            raise RuntimeError(
                f"post-update check failed: {check['error']}\n"
                "The service was NOT restarted, so the previous version is still "
                f"running. Restore the checkout with: git -C {repo} reset --hard "
                f"{prev}\nand reinstall the ROCm torch build if it was replaced.")

        restarted = False
        st = systemd.unit_state(paths.COMFY_UNIT)
        if restart and st.get("ActiveState") == "active":
            log("restarting ai-comfyui")
            systemd.service_action("comfyui", "restart")
            ok = self._wait_healthy(log, wait=120)
            if not ok:
                raise RuntimeError(
                    "service did not become healthy after update — it may need "
                    f"`git -C {repo} reset --hard {prev}` and a restart")
            restarted = True
        log(f"rollback note: git reset --hard {prev[:12]} restores this "
            f"version (pip packages are NOT uninstalled by a rollback)")
        return {"status": "success", "prev": prev, "commit": new,
                "restarted": restarted, "python": python,
                # where that interpreter came from: "which venv did this touch?"
                # must be answerable from the job, not from memory
                "python_src": python_src,
                "verified": check,
                "changed": {n: {"from": a, "to": b} for n, (a, b) in changed.items()},
                "held": pins}

    def endpoint(self) -> str:
        """Base URL the portal uses to reach ComfyUI — honours the bind from
        the launch env, so a LAN-only bind stays probeable (a daemon bound to
        192.168.x.x does not answer on 127.0.0.1)."""
        return serviceconf.base_url("comfyui",
                                   default_port=self.config()["port"])

    def _wait_healthy(self, log, wait: int = 120) -> bool:
        url = self.endpoint() + "/system_stats"
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=3)
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 — still starting
                pass
            time.sleep(3)
        return False

    def rollback(self, commit: str, restart: bool = True, log=print) -> dict:
        """git reset --hard to a known commit + restart (pip is not rolled
        back — see update())."""
        repo = paths.COMFY_REPO
        r = subprocess.run(["git", "-C", str(repo), "reset", "--hard", commit],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"git reset failed:\n{r.stderr.strip()[:400]}")
        log(f"reset to {commit[:12]}")
        restarted = False
        st = systemd.unit_state(paths.COMFY_UNIT)
        if restart and st.get("ActiveState") == "active":
            systemd.service_action("comfyui", "restart")
            self._wait_healthy(log, wait=120)
            restarted = True
        return {"status": "success", "commit": commit, "restarted": restarted}

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
