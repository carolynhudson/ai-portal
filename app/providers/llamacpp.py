"""llama.cpp provider: release registry, deployed binary, router service, models."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .base import Provider, ToolStatus
from ..core import (db, health, llamakey, modelindex, paths, serviceconf, systemd,
                    units, updater)
from ..core.modeladd import _AUX_PREFIX_RE   # what counts as a model: one opinion


def _git_head(repo: str | Path) -> str | None:
    """HEAD of a repo, or None if git cannot answer.

    Accepts str or Path: every call site holds a `Path` from `paths`, and the body
    stringifies for argv anyway, so the narrower annotation described no caller.
    """
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
        return lines[1].split()[0] if len(lines) >= 2 else None
    except Exception:  # noqa: BLE001
        return None


def _router_models() -> list[dict]:
    """Live model registry from the router as it is *configured* (env file),
    sending the API key when one is set."""
    try:
        import httpx
        r = httpx.get(f"{serviceconf.base_url('llamacpp', 8080)}/v1/models",
                      timeout=8, headers=llamakey.request_headers())
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
        lines = _version_lines((out.stdout or "") + (out.stderr or ""))
        # The version line, not the first line: a logger preamble is not this box's
        # llama.cpp version and showing it in the UI is how a wrong number gets trusted.
        return lines[0] if lines else None
    except Exception:  # noqa: BLE001
        return None


_BEHIND_CACHE = (0.0, None)


def _release_url(tag: str) -> str | None:
    """GitHub release-notes URL for a tag, derived from the remote URL."""
    try:
        url = _git(paths.LLAMA_REPO, "remote", "get-url", "origin",
                   log=lambda m: None).strip()
    except RuntimeError:
        return None
    url = re.sub(r"\.git$", "", url)
    m = re.match(r"^(?:https?://|git@)?([^/:]+)[:/](.+)$", url)
    if not m:
        return None
    host, repo_path = m.groups()
    return f"https://{host}/{repo_path}/releases/tag/{tag}"


_REMOTE_CACHE_KEY = "llamacpp_remote_state"


def _cache_read() -> dict:
    v = db.get_setting(_REMOTE_CACHE_KEY)
    return v if isinstance(v, dict) else {}


def _cache_write(state: dict) -> None:
    # direct upsert: routine cache writes shouldn't spam the audit log
    db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (_REMOTE_CACHE_KEY, json.dumps(state)))


def _remote_state_probe(force: bool = False) -> dict:
    """Upstream probe: how far is the *deployed* release behind origin's
    default branch and the latest v-tag (the GitHub release)?

    Cached for ``remote_check_ttl_hours`` (default 6h, a few checks a day);
    the check-now button forces a fresh probe. Offline -> last good state,
    flagged ``stale``.
    """
    cached = _cache_read()
    ttl = float(db.get_setting("remote_check_ttl_hours", 6.0) or 6.0)
    if not force and cached and time.time() - cached.get("checked_at", 0) < ttl * 3600:
        return cached
    repo = paths.LLAMA_REPO
    if not (repo / ".git").exists():
        return {"error": "llama.cpp repo missing"}
    try:
        _run(lambda m: None, ["git", "-C", str(repo), "fetch", "--tags",
                              "--quiet", "origin"], timeout=300)
    except RuntimeError:
        st = dict(cached)
        st.update({"stale": True, "error": "fetch failed (offline?)"})
        return st
    try:
        head = _git(repo, "rev-parse", "--verify", "origin/HEAD^{commit}",
                    log=lambda m: None).strip()
    except RuntimeError:
        head = _git(repo, "rev-parse", "--verify", "origin/master^{commit}",
                    log=lambda m: None).strip()
    reg = releases()
    cur = next((r for r in reg.get("releases", [])
                if r.get("name") == reg.get("current")), None)
    deployed = (cur or {}).get("commit")

    def _count(ref: str) -> int | None:
        if not deployed:
            return None
        try:
            out = _git(repo, "rev-list", "--count", f"{deployed}..{ref}",
                       log=lambda m: None)
            return int(out.strip())
        except RuntimeError:
            return None

    tags = _git(repo, "tag", "-l", "v[0-9]*", "--sort=-v:refname",
                log=lambda m: None).split()
    latest = tags[0] if tags else None
    release_commit = None
    behind_release = None
    if latest:
        try:
            release_commit = _git(repo, "rev-parse", "--verify",
                                  f"{latest}^{{commit}}",
                                  log=lambda m: None).strip()
            behind_release = _count(f"{latest}^{{commit}}")
        except RuntimeError:
            release_commit = None
    state = {
        "checked_at": time.time(),
        "master_head": head,
        "behind_master": _count(head),
        "latest_release": latest,
        "release_commit": release_commit,
        "behind_release": behind_release,
        "release_url": _release_url(latest) if latest else None,
        "update_available": bool(behind_release and behind_release > 0)
                            or bool(_count(head)),
    }
    _cache_write(state)
    return state


def behind_by() -> dict:
    """Local repo HEAD vs the remote default branch.

    Delegates to :func:`remote_state` (TTL-cached fetch + count) so the
    dashboard's health line and the release bubble share one probe.
    """
    st = _remote_state_probe()
    return {"local": _git_head(paths.LLAMA_REPO),
            "origin": st.get("master_head"),
            "behind": st.get("behind_master")}


# --------------------------------------------------------------- launch config
# The router is launched the same way ComfyUI is: static unit -> env file ->
# generated wrapper. Everything the Settings page owns lives in the env file:
#
#   LLAMA_HOST / LLAMA_PORT        bind + port (serviceconf writes these)
#   LLAMA_MODELS_MAX               router slots (default 1)
#   LLAMA_MODELS_DIR / LLAMA_PRESETS  model store + per-model tuning
#   LLAMA_METRICS                  "1" adds --metrics
#   LLAMA_API_KEY_FILE             optional --api-key-file (see core.llamakey)
#   LLAMA_ARGS                     free-form extra args (word-split, no quoting)
#   HF_HOME                        kept per-service (finding F11)
ROUTER_ENV_DEFAULTS: dict[str, str] = {
    "LLAMA_BIN": str(paths.AI_BIN / "llama-server"),
    "LLAMA_HOST": paths.ROUTER_HOST,
    "LLAMA_PORT": str(paths.ROUTER_PORT),
    "LLAMA_METRICS": "1",
    "LLAMA_MODELS_MAX": "1",
    "LLAMA_MODELS_DIR": str(paths.LLAMA_MODELS),
    "LLAMA_PRESETS": str(paths.PRESETS_INI),
    "LLAMA_ARGS": "",
    "HF_HOME": str(paths.HF_CACHE),
}

# NOTE: `set --` builds argv positionally so every value keeps its quoting;
# only $LLAMA_ARGS is deliberately word-split (same contract as COMFY_ARGS).
# AI_PORTAL_PRINT_ARGV=1 prints the argv instead of exec'ing, which is how the
# portal verifies a launch config without loading a 30 GB model.
# Substitution uses @@TOKEN@@ rather than %- or {}-formatting: the shell body is
# full of ${VARS} and printf '%s', which those formatters would eat.
ROUTER_WRAPPER = """#!/bin/sh
# AI portal - llama.cpp router launch wrapper (GENERATED, do not hand-edit).
# Every parameter comes from ~/ai/config/llamacpp.env; edit it via the portal's
# Settings page, then restart ai-llama-router to apply. The defaults below are
# only a safety net — the env file is what the portal writes and owns.
set -eu

LLAMA_BIN="${LLAMA_BIN:-@@bin@@}"
if [ ! -x "$LLAMA_BIN" ]; then
  echo "ai-llama-router: no executable at $LLAMA_BIN (deploy a release first)" >&2
  exit 1
fi

set -- --host "${LLAMA_HOST:-@@host@@}" --port "${LLAMA_PORT:-@@port@@}"
if [ "${LLAMA_METRICS:-1}" = "1" ]; then set -- "$@" --metrics; fi
if [ -n "${LLAMA_API_KEY_FILE:-}" ] && [ -f "${LLAMA_API_KEY_FILE}" ]; then
  set -- "$@" --api-key-file "${LLAMA_API_KEY_FILE}"
fi
set -- "$@" --models-max "${LLAMA_MODELS_MAX:-@@models_max@@}" \\
           --models-dir "${LLAMA_MODELS_DIR:-@@models_dir@@}" \\
           --models-preset "${LLAMA_PRESETS:-@@presets@@}"
# free-form extra args: intentionally word-split (no quoting supported)
for a in ${LLAMA_ARGS:-}; do set -- "$@" "$a"; done

# AI_PORTAL_PRINT_ARGV=1 prints the argv instead of exec'ing llama-server: the
# portal uses it to verify a launch config without loading a model.
if [ "${AI_PORTAL_PRINT_ARGV:-0}" = "1" ]; then
  printf '%s\\n' "$LLAMA_BIN" "$@"
  exit 0
fi
exec "$LLAMA_BIN" "$@"
"""


def render_router_wrapper(env: dict[str, str]) -> str:
    """The wrapper text with this machine's current values as shell defaults."""
    return (ROUTER_WRAPPER
            .replace("@@bin@@", env["LLAMA_BIN"])
            .replace("@@host@@", env["LLAMA_HOST"])
            .replace("@@port@@", str(env["LLAMA_PORT"]))
            .replace("@@models_max@@", str(env["LLAMA_MODELS_MAX"]))
            .replace("@@models_dir@@", env["LLAMA_MODELS_DIR"])
            .replace("@@presets@@", env["LLAMA_PRESETS"]))


def router_env() -> dict[str, str]:
    """Effective launch env (file values over defaults), defaults first."""
    return {**ROUTER_ENV_DEFAULTS, **serviceconf.read_service_env("llamacpp")}


def router_argv(env: dict[str, str] | None = None) -> list[str]:
    """The argv the wrapper would exec, computed in Python (used to preview
    and to assert the wrapper and the portal agree)."""
    e = env or router_env()
    argv = [e["LLAMA_BIN"], "--host", e["LLAMA_HOST"], "--port", str(e["LLAMA_PORT"])]
    if e.get("LLAMA_METRICS", "1") == "1":
        argv.append("--metrics")
    key_file = e.get(llamakey.ENV_KEY, "")
    if key_file and Path(key_file).is_file():
        argv += ["--api-key-file", key_file]
    argv += ["--models-max", str(e["LLAMA_MODELS_MAX"]),
             "--models-dir", e["LLAMA_MODELS_DIR"],
             "--models-preset", e["LLAMA_PRESETS"]]
    argv += [a for a in (e.get("LLAMA_ARGS") or "").split() if a]
    return argv


class LlamaCpp(Provider):
    key, name, kind = "llamacpp", "llama.cpp", "llamacpp"

    # -------------------------------------------------------- service launch
    def ensure_service(self, log=print) -> dict:
        """Generate env file + wrapper + static unit for the router (idempotent).

        Mirrors :meth:`ComfyUI.ensure_service`. Does NOT start or restart
        anything: swapping the router is a confirmed operation (the agent
        session may be riding it). The previous unit file is kept under
        ``state/rollback/units/`` by :func:`units.install_unit`.
        """
        env = router_env()
        units.write_env("llamacpp", env, log)
        units.write_wrapper("llamacpp", render_router_wrapper(env), log)
        unit_info = units.install_unit(paths.ROUTER_UNIT, "llamacpp",
                                       "llama.cpp model router (AI Portal)",
                                       str(paths.AI_ROOT), log) or {}
        verify = verify_wrapper_argv()
        return {"unit": paths.ROUTER_UNIT,
                "env": str(units.env_path("llamacpp")),
                "wrapper": str(units.wrapper_path("llamacpp")),
                "unit_file": unit_info.get("unit_file"),
                "unit_backup": unit_info.get("backup"),
                "argv": verify.get("argv"), "verify": verify, "config": env}


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

    # ------------------------------------------------------------------ C
    # Update pipeline (productionizes tools/phase3_rebuild_llama_cpp.py + the
    # phase-4 swap): fetch -> resolve ref -> build in ~/ai/build/ -> stage a
    # self-contained release in ~/ai/releases/ -> verify (version, ldd, smoke
    # worker) -> repoint the ~/ai/bin symlink farm -> restart the router.
    # The previous release stays on disk until a one-click rollback replaces
    # it again. See docs/PORTAL_PLAN.md §4.4.

    def provenance(self) -> dict:
        """What the deployed release was built against — **local reads only**.

        The health pane needs this on page load. ``update_status()`` refreshes
        the upstream probe whenever its cache is stale, which can mean a
        ``git fetch`` of up to 300 s: a read-only pane must not be able to block
        for minutes, so it reads the last saved probe instead. The Status page's
        check button is what costs a fetch.
        """
        reg = releases()
        cur = next((r for r in reg.get("releases", [])
                    if r.get("name") == reg.get("current")), {}) or {}
        sdk = (cur.get("vulkan_sdk") or "").rstrip("/").split("/")[-1]
        remote = _cache_read()
        return {
            "name": reg.get("current"),
            "commit": (cur.get("commit") or "")[:7],
            "built_at": cur.get("built_at"),
            "rocm_built": cur.get("rocm"),
            "vulkan_built": sdk or None,
            "repo_head": _git_head(paths.LLAMA_REPO),
            "release_count": len(reg.get("releases") or []),
            "behind_master": remote.get("behind_master"),
            "latest_release": remote.get("latest_release"),
            "remote_checked": remote.get("checked_at"),
            "remote_stale": remote.get("stale"),
        }

    def update_status(self) -> dict:
        """Everything the dashboard's release bubble needs.

        Each release carries its size and an ``active`` flag: the dashboard lists
        builds in one dropdown and offers to delete the selected one, so the page
        needs to know which entry is locked and what deleting it would free."""
        reg = releases()
        current = reg.get("current")
        known = []
        for r in reg.get("releases", []):
            e = dict(r)
            e["active"] = e.get("name") == current
            size = release_size(e.get("path"))
            e["size_bytes"] = size
            e["size_mb"] = round(size / 1048576, 1) if size is not None else None
            known.append(e)
        active = next((e for e in known if e["active"]), None) or {}
        # Unregistered builds ride along with the list they should have been on:
        # a build nobody can see is a build nobody can delete, and the pane that
        # shows builds is where a person would look. A failed scan must not take
        # the release list down with it — that list is how you switch builds.
        try:
            strays = stray_releases()
        except Exception as e:  # noqa: BLE001 — degraded, not fatal
            strays = {"strays": [], "unexpected": [], "bytes": 0,
                      "error": f"{type(e).__name__}: {e}"}
        return {
            "current": current,
            "current_commit": active.get("commit"),
            "current_size_bytes": active.get("size_bytes"),
            "strays": strays["strays"],
            "stray_bytes": strays["bytes"],
            "unexpected_builds": strays["unexpected"],
            # empty strays and "could not check" must never look the same: the
            # first means there is nothing to purge, the second means nobody knows
            "stray_error": strays.get("error"),
            "repo_head": _git_head(paths.LLAMA_REPO),
            "releases": known,
            "release_count": len(known),
            "total_bytes": sum(e["size_bytes"] or 0 for e in known),
            "vulkan_sdk": str(paths.VULKAN_SDK),
            "binary": _binary_version(),
            "source": str(paths.LLAMA_REPO),
            "remote": _remote_state_probe(),
            "job": updater.latest_job(self.key),
        }

    def remote_state(self, force: bool = False) -> dict:
        """Upstream probe (master + latest release) for the release bubble."""
        return _remote_state_probe(force=force)

    def preflight(self) -> dict:
        """Fast local checks done *before* a job is started, so the API can
        reject obviously-bad updates without a 10-minute build finding out."""
        repo = paths.LLAMA_REPO
        if not (repo / ".git").exists():
            raise ValueError(f"llama.cpp repo missing: {repo}")
        dirty = _git(repo, "status", "--porcelain",
                     log=lambda m: None).strip()
        if dirty:
            raise ValueError("llama.cpp repo is dirty — commit or stash "
                             "changes before updating:\n" + dirty[:400])
        for tool in ("git", "cmake", "ninja"):
            if not shutil.which(tool):
                raise ValueError(f"missing toolchain component: {tool}")
        return {"ok": True, "head": _git_head(repo)}

    def update(self, target: str = "HEAD", restart: bool = True,
               smoke: bool = True, dry: bool = False, log=print) -> dict:
        """Run the full pipeline. Any failure leaves the running release
        untouched; ``dry=True`` stops after staging+verify (nothing deployed).
        """
        repo = paths.LLAMA_REPO
        # 1. refresh refs (offline -> fall back to local refs)
        try:
            _git(repo, "fetch", "--tags", "origin", log=log, timeout=300)
        except RuntimeError as e:
            log(f"WARNING: git fetch failed (continuing with local refs): {e}")
        # 2. resolve the target to a commit
        commit = self._resolve(target, log)
        # 3. refuse to build over local edits
        dirty = _git(repo, "status", "--porcelain", log=log).strip()
        if dirty:
            raise RuntimeError("llama.cpp repo went dirty mid-update — "
                               "aborted before checkout\n" + dirty[:400])
        # 4. no-op: the target IS the deployed release
        reg = releases()
        current = reg.get("current")
        cur_entry = next((r for r in reg.get("releases", [])
                          if r.get("name") == current), None)
        if (cur_entry or {}).get("commit", "") == commit:
            log(f"no-op: {commit[:12]} is already the deployed release "
                f"({current}) — nothing to do")
            return {"status": "noop", "release": current, "commit": commit}
        # 5. checkout + configure + build
        _git(repo, "checkout", "--quiet", commit, log=log)
        build_dir = paths.BUILD_ROOT / f"llama.cpp-{commit[:12]}"
        build_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "VULKAN_SDK": str(paths.VULKAN_SDK)}
        _hipcc_shim_env(env, log)
        if not (build_dir / "CMakeCache.txt").exists():
            log(f"configuring in {build_dir} (SDK {paths.VULKAN_SDK.name})")
            _run(log, ["cmake", "-S", str(repo), "-B", str(build_dir),
                       "-G", "Ninja", *paths.LLAMA_CMAKE_ARGS], env=env)
        log(f"building (this is the slow step, ~10-20 min, "
            f"{os.cpu_count()} jobs)")
        _run(log, ["cmake", "--build", str(build_dir),
                   "-j", str(os.cpu_count() or 4)], env=env)
        server = build_dir / "bin" / "llama-server"
        if not server.exists():
            raise RuntimeError(f"build finished but {server} is missing")
        ver_out = _run(log, [str(server), "--version"])
        ver_lines = _version_lines(ver_out)
        ver_line = " | ".join(ver_lines)
        m = _VER_RE.search(ver_line)
        if not m:
            # Keep the whole output in the message. The failure mode this replaces said
            # only the first line, which was a logger line and told the operator nothing
            # about what the binary actually reported.
            raise RuntimeError(f"unexpected --version output: {ver_line or ver_out.strip()[:300]}")
        log(f"built: {ver_line}")
        # 6. stage a self-contained release (flat: binaries + libs together,
        #    RPATH=$ORIGIN — same layout as the phase-3 release)
        rel_name = f"llama.cpp-b{m.group(1)}"
        rel_dir = paths.RELEASES / rel_name
        if rel_dir.exists():
            log(f"re-staging existing release dir {rel_dir.name}")
            shutil.rmtree(rel_dir)
        rel_dir.mkdir(parents=True)
        n = 0
        for f in sorted((build_dir / "bin").iterdir()):
            if f.is_file():
                shutil.copy2(f, rel_dir / f.name)
                n += 1
        for f in build_dir.glob("*.so*"):
            if f.is_file():
                shutil.copy2(f, rel_dir / f.name)
                n += 1
        log(f"staged {n} files -> {rel_dir}")
        # 7. verify the staged release before anything touches production
        staged_lines = _version_lines(_run(log, [str(rel_dir / "llama-server"), "--version"]))
        staged = " | ".join(staged_lines)
        if m.group(2) not in staged:
            raise RuntimeError(f"staged version mismatch: {staged}")
        ldd = _run(log, ["ldd", str(rel_dir / "llama-server")])
        bad = [ln for ln in ldd.splitlines() if "not found" in ln]
        if bad:
            raise RuntimeError("release has unresolved libraries:\n"
                               + "\n".join(bad))
        log("verify: version ok, ldd clean")
        if smoke:
            self._smoke_worker(rel_dir, log)
        if dry:
            log(f"dry run: release staged at {rel_dir} but NOT deployed "
                f"(bin farm and router untouched)")
            return {"status": "dry-run", "release": rel_name, "commit": commit,
                    "path": str(rel_dir)}
        # 8. swap: rollback note + repoint ~/ai/bin + releases.json
        self._swap_release(reg, cur_entry, rel_name, rel_dir, commit, log)
        db.record_release(self.key, rel_name, str(rel_dir), commit,
                          str(paths.VULKAN_SDK), active=True)
        # 9. restart the router (the update request is the confirmation)
        if restart:
            _run(log, ["systemctl", "--user", "restart", paths.ROUTER_UNIT])
            if not _router_healthy(log, wait=90):
                prev = cur_entry["name"] if cur_entry else None
                if prev:
                    log(f"router not healthy on {rel_name} — auto-rolling "
                        f"back to {prev}")
                    self._repoint(paths.RELEASES / prev, log)
                    reg["current"] = prev
                    paths.RELEASES_JSON.write_text(json.dumps(reg, indent=2) + "\n")
                    db.set_release_active(self.key, rel_name, active=False)
                    db.set_release_active(self.key, prev, active=True)
                    _run(log, ["systemctl", "--user", "restart", paths.ROUTER_UNIT],
                         timeout=300)
                raise RuntimeError("router is not healthy after the update"
                                   + (" (auto-rollback attempted)" if prev
                                      else " (no previous release to roll back to)"))
            log("router restarted and answering on :8080")
        return {"status": "success", "release": rel_name, "commit": commit,
                "restarted": restart}

    def rollback(self, release: str, restart: bool = True, log=print) -> dict:
        """Fast: repoint the bin farm at a known release dir and restart."""
        reg = releases()
        entry = next((r for r in reg.get("releases", [])
                      if r.get("name") == release), None)
        if entry is None:
            known = ", ".join(r.get("name", "?") for r in reg.get("releases", []))
            raise ValueError(f"unknown release {release!r} (known: {known or 'none'})")
        rel_dir = Path(entry.get("path") or "")
        if not (rel_dir / "llama-server").exists():
            raise ValueError(f"release directory missing: {rel_dir}")
        if reg.get("current") == release:
            log(f"no-op: {release} is already current")
            return {"status": "noop", "release": release}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rb = paths.ROLLBACK_DIR / f"llamacpp-rollback-{stamp}"
        rb.mkdir(parents=True, exist_ok=True)
        (rb / "releases.json").write_text(json.dumps(reg, indent=2) + "\n")
        (rb / "note.txt").write_text(
            f"rollback {reg.get('current')} -> {release} at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._repoint(rel_dir, log)
        reg["current"] = release
        paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
        paths.RELEASES_JSON.write_text(json.dumps(reg, indent=2) + "\n")
        db.set_release_active(self.key, release, active=True)
        if restart:
            _run(log, ["systemctl", "--user", "restart", paths.ROUTER_UNIT])
            if not _router_healthy(log, wait=90):
                raise RuntimeError(f"router not healthy after rollback to {release}")
        return {"status": "success", "release": release, "restarted": restart}

    def delete_release(self, release: str, log=print) -> dict:
        """Delete a **non-current** release directory and forget it from the
        registry. This is the one thing the portal does to disk permanently, so
        every guard is a refusal, not a warning:

        * the name must be in the registry — the registry is the only source of
          paths, so an API caller cannot aim the delete anywhere;
        * the current release is untouchable (the router runs from it) and so is
          any release the ``~/ai/bin`` farm still points at, even if a
          hand-edited registry claims otherwise;
        * the path must live under ``~/ai/releases``;
        * no update job may be running (one could be building into it).

        The registry is copied to the rollback dir first, and the audit log gets
        an event with the size freed.
        """
        if (updater.latest_job(self.key) or {}).get("status") == "running":
            raise ValueError("an update job is running — try again when it finishes")

        reg = releases()
        entry = next((r for r in reg.get("releases", [])
                      if r.get("name") == release), None)
        if entry is None:
            known = ", ".join(r.get("name", "?") for r in reg.get("releases", []))
            raise ValueError(f"unknown release {release!r} (known: {known or 'none'})")
        if reg.get("current") == release:
            raise ValueError(f"{release} is the current build — switch to another "
                             f"build before deleting it")

        rel_dir = Path(entry.get("path") or "")
        root = paths.RELEASES.resolve()
        if not rel_dir.is_dir():
            raise ValueError(f"release directory missing: {rel_dir}")
        resolved = rel_dir.resolve()
        if resolved.parent != root or resolved == root:
            raise ValueError(f"refusing to delete outside {root}: {resolved}")

        # the farm, not the registry, is the truth about what is executing: the
        # links point *into* a release directory, so match containment, not
        # equality — a link to .../llama.cpp-b1/llama-server holds b1
        held = sorted(l.name for l in (paths.AI_BIN.glob("*") if paths.AI_BIN.is_dir() else [])
                      if l.is_symlink() and _points_into(_link_target(l), resolved))
        if held:
            raise ValueError(f"~/ai/bin still points at {resolved} "
                             f"({', '.join(held)}) — not deleting")

        size = release_size(resolved, force=True) or 0
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rb = paths.ROLLBACK_DIR / f"llamacpp-delete-{stamp}"
        rb.mkdir(parents=True, exist_ok=True)
        (rb / "releases.json").write_text(json.dumps(reg, indent=2) + "\n")
        (rb / "note.txt").write_text(
            f"deleted release {release} ({resolved}, "
            f"{size / 1048576:.1f} MiB, commit {(entry.get('commit') or '')[:12]}, "
            f"built {entry.get('built_at')}) at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"registry before deletion is releases.json\n")

        shutil.rmtree(resolved)
        reg["releases"] = [r for r in reg.get("releases", [])
                           if r.get("name") != release]
        paths.RELEASES_JSON.write_text(json.dumps(reg, indent=2) + "\n")
        db.set_release_active(self.key, release, active=False)
        log(f"deleted {resolved} ({size / 1048576:.1f} MiB freed); "
            f"{len(reg['releases'])} release(s) remain")
        return {"status": "success", "deleted": release, "freed_bytes": size,
                "remaining": len(reg["releases"]), "note_dir": str(rb)}

    def stray_builds(self) -> dict:
        """Unregistered build directories, for the health pane (see the module
        function). A method so callers stay provider-agnostic."""
        return stray_releases()

    def purge_stray(self, name: str, log=print) -> dict:
        """Delete an **unregistered** release directory: built here, unknown to
        the registry, invisible to every list in the UI.

        This is the permanently-destructive path with less to lean on than
        ``delete_release``, so the guards are re-derived from disk here rather
        than trusted from whatever the caller was shown a moment ago:

        * the name must have the shape of a release the portal builds — that plus
          containment under ``~/ai/releases`` is the whole authority, since the
          registry cannot vouch for a build it has never seen;
        * a *registered* release is refused: it has ``delete build`` for that,
          which also forgets the entry and keeps the registry consistent;
        * the ``~/ai/bin`` farm may not point into it, and no process may be
          executing from it — checked now, not assumed from page state;
        * if ``/proc`` cannot be read, nothing is deleted. "Couldn't check" is
          not evidence of safety.

        What it was gets written down first — size, file manifest, and the
        ``--version`` of the binary it contained — because after this there is
        nothing left to ask.
        """
        if (updater.latest_job(self.key) or {}).get("status") == "running":
            raise ValueError("an update job is running — try again when it finishes")

        if not _REL_DIR_RE.match(name or ""):
            raise ValueError(
                f"{name!r} is not the name of a build directory "
                f"(expected llama.cpp-b<build>)")
        reg = releases()
        if any(r.get("name") == name for r in reg.get("releases", [])):
            raise ValueError(f"{name} is a registered build — use “Delete build”, "
                             f"which also removes it from the registry")

        root = paths.RELEASES.resolve()
        target = (root / name)
        if not target.is_dir():
            raise ValueError(f"no such build directory: {target}")
        resolved = target.resolve()
        if resolved.parent != root or resolved == root:
            raise ValueError(f"refusing to delete outside {root}: {resolved}")

        held = _farm_links_into(resolved)
        if held:
            raise ValueError(
                f"~/ai/bin still points at {resolved} ({_farm_note(held)}) — "
                f"this build is in use, not deleting")
        pids = _procs_using(resolved)
        if pids is None:
            raise ValueError("cannot verify that nothing is running from "
                             f"{resolved} (no readable /proc) — not deleting")
        if pids:
            raise ValueError(f"a process is running from {resolved} "
                             f"(pid {', '.join(map(str, pids))}) — not deleting")

        # Record what is about to be destroyed: the registry never knew it, so
        # this note is the only surviving answer to "what was that".
        files = sorted(p.relative_to(resolved).as_posix()
                       for p in resolved.rglob("*") if p.is_file())
        size = release_size(resolved, force=True) or 0
        version = ""
        exe = resolved / "llama-server"
        if exe.is_file():
            try:
                r = subprocess.run([str(exe), "--version"], capture_output=True,
                                   text=True, timeout=20)
                lines = _version_lines((r.stdout or r.stderr or ""))
                version = lines[0] if lines else ""
            except Exception as e:  # noqa: BLE001 — a stray build need not cooperate
                version = f"(version check failed: {type(e).__name__}: {e})"

        stamp = time.strftime("%Y%m%d-%H%M%S")
        rb = paths.ROLLBACK_DIR / f"llamacpp-purge-{stamp}"
        rb.mkdir(parents=True, exist_ok=True)
        (rb / "note.txt").write_text(
            f"purged UNREGISTERED build {name} ({resolved})\n"
            f"{len(files)} file(s), {size / 1048576:.1f} MiB\n"
            f"binary reported: {version or '(no llama-server in the directory)'}\n"
            f"directory mtime: {datetime.fromtimestamp(resolved.stat().st_mtime, timezone.utc).isoformat()}\n"
            f"purged at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} by ai-portal\n"
            f"this build was never in ~/ai/state/llama-releases.json\n")
        (rb / "manifest.txt").write_text("\n".join(files) + "\n")

        shutil.rmtree(resolved)
        log(f"purged {resolved} ({size / 1048576:.1f} MiB freed, "
            f"{len(files)} files); record in {rb}")
        return {"status": "success", "purged": name, "freed_bytes": size,
                "files": len(files), "version": version, "note_dir": str(rb)}

    # ------------------------------------------------------------ internals

    def _resolve(self, target: str, log) -> str:
        """Resolve HEAD / a tag / a sha to a full commit (after the fetch)."""
        t = (target or "").strip() or "HEAD"
        if t == "HEAD":
            # 'HEAD' means the remote default branch when reachable, else local
            try:
                out = _git(paths.LLAMA_REPO, "ls-remote", "--symref",
                           "origin", "HEAD", log=lambda m: None, timeout=60)
                for ln in out.splitlines():
                    # symref line: "ref: refs/heads/<branch><TAB>HEAD"
                    parts = ln.split()
                    if (len(parts) >= 2 and parts[0] == "ref:"
                            and parts[1].startswith("refs/heads/")):
                        t = "origin/" + parts[1][len("refs/heads/"):]
                        break
            except RuntimeError:
                log("no remote info — 'HEAD' means local HEAD")
        try:
            out = _git(paths.LLAMA_REPO, "rev-parse", "--verify", f"{t}^{{commit}}",
                       log=log)
        except RuntimeError:
            raise ValueError(f"cannot resolve target ref: {t!r}")
        commit = out.strip().splitlines()[0]
        log(f"target {t} -> {commit[:12]}")
        return commit

    def _swap_release(self, reg: dict, prev_entry, rel_name: str,
                      rel_dir: Path, commit: str, log) -> None:
        """Rollback note, repoint ~/ai/bin, update releases.json."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rb = paths.ROLLBACK_DIR / f"llamacpp-{stamp}"
        rb.mkdir(parents=True, exist_ok=True)
        (rb / "releases.json").write_text(json.dumps(reg, indent=2) + "\n")
        farm = sorted(os.listdir(paths.AI_BIN)) if paths.AI_BIN.is_dir() else []
        (rb / "bin-farm.txt").write_text("\n".join(farm) + "\n")
        (rb / "note.txt").write_text(
            f"update {reg.get('current')} -> {rel_name} (commit {commit}) at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._repoint(rel_dir, log)
        entry = {"name": rel_name, "commit": commit,
                 "version": rel_name.rsplit("b", 1)[-1],
                 "path": str(rel_dir), "vulkan_sdk": str(paths.VULKAN_SDK),
                 # the ROCm this compiled against, stamped now: after an apt
                 # upgrade there is no way to recover it from the binary, and
                 # "rebuild for the new ROCm?" is the question the health pane
                 # has to answer (docs/UPDATES.md, version-coupling watch items)
                 "rocm": health.installed_rocm(),
                 "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if rel_name in {r.get("name") for r in reg.get("releases", [])}:
            reg["releases"] = [e if e.get("name") == rel_name else e
                               for e in reg.get("releases", [])]
        else:
            reg.setdefault("releases", []).append(entry)
        reg["current"] = rel_name
        paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
        paths.RELEASES_JSON.write_text(json.dumps(reg, indent=2) + "\n")
        log(f"swapped: current release is now {rel_name} "
            f"(previous kept at {prev_entry.get('path') if prev_entry else 'n/a'})")

    def _repoint(self, rel_dir: Path, log) -> None:
        """Repoint every entry of the ~/ai/bin symlink farm at rel_dir."""
        paths.AI_BIN.mkdir(parents=True, exist_ok=True)
        for exe in sorted(rel_dir.iterdir()):
            if not exe.is_file() or exe.name.startswith("lib"):
                continue
            link = paths.AI_BIN / exe.name
            if link.is_symlink():
                if link.resolve() == exe.resolve():
                    continue
                link.unlink()
            elif link.exists():
                link.unlink()   # a plain file in the farm — replace it
            link.symlink_to(exe)
        for link in paths.AI_BIN.iterdir():
            if link.is_symlink() and not link.exists():
                log(f"removing dangling farm entry {link.name}")
                link.unlink()
        log(f"symlink farm -> {rel_dir}")

    def _smoke_worker(self, rel_dir: Path, log) -> None:
        """Boot a throwaway worker from the staged release and wait for /health.

        Proves the release runs *here* — which is the whole gate in front of a
        deploy, so it must not be able to fail for a reason unrelated to the
        build. Picking the model is therefore not a detail:

        Live, this took `min(every *.gguf, key=size)` and chose
        `Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf` at 11 MB — the first
        shard of a 94 GB model, which is the smallest *file* on disk and nowhere
        near the smallest *model*. llama.cpp read its header (which describes the
        whole model) and asked for a 37 GB buffer on a box with 14 GB free. The
        build was fine; the update was reported as failed.

        So: shards are treated as one model at their full size, projectors and
        imatrix files are not models at all, candidates have to fit the memory
        actually available, and a failure that says "could not allocate" is
        reported as the environment problem it is instead of blaming the build.
        """
        avail = _mem_available()
        # Prefer the index: it orders by weights + estimated KV, not by file size,
        # so the model it picks has room for the cache it is about to allocate.
        cands = _indexed_candidates(avail, log) or _model_candidates(paths.LLAMA_MODELS)
        if not cands:
            log("no models on disk — skipping smoke worker")
            return
        fits = [c for c in cands if c[1] + _SMOKE_HEADROOM <= avail]
        too_big = cands[len(fits):]
        if too_big:
            log(f"smoke worker: {len(too_big)} model(s) too large for the "
                f"{_sz(avail)} free, smallest skipped: "
                + ", ".join(f"{c[2]} ({_sz(c[1])})" for c in too_big[:3]))
        if not fits:
            raise RuntimeError(
                f"smoke test could not run: no model on disk fits in the "
                f"{_sz(avail)} of memory available (smallest is "
                f"{cands[0][2]} at {_sz(cands[0][1])}). The build itself "
                f"verified clean and is staged at {rel_dir} — it was NOT "
                f"deployed. Free memory, or update with smoke off.")
        last = None
        for path, size, label in fits[:_SMOKE_ATTEMPTS]:
            ok, out = _run_worker(rel_dir, path, log, label, size)
            if ok:
                log("smoke worker OK")
                return
            if _is_memory_error(out):
                last = (label, out)
                log(f"smoke worker: {label} could not get memory — "
                    f"{'trying a smaller model' if len(fits) > 1 else 'no smaller model left'}")
                continue
            log("smoke worker output (tail):")
            log(out[-1500:] or "(no output)")
            raise RuntimeError(f"smoke worker failed for {label}: the staged "
                               f"release did not serve /health (output above)")
        label, out = last
        log("smoke worker output (tail):")
        log(out[-1500:] or "(no output)")
        raise RuntimeError(
            f"smoke test could not get memory for any candidate (last: {label}). "
            f"The build verified clean but is NOT deployed — something is holding "
            f"the memory this box needs. See the tail above.")

# ------------------------------------------------------------------ smoke test
#
# What a candidate model is, and what it costs. Both halves of that have to be
# right or the gate in front of a deploy fails for reasons that have nothing to
# do with the build.

_SMOKE_ATTEMPTS = 3        # candidates to try, smallest first
_SMOKE_HEADROOM = 2 << 30  # a model this much smaller than free memory still fits

# Below this a .gguf is not a model: an LFS pointer, a placeholder, a download
# that died in its first seconds. Not a size ranking — a sanity floor, which is
# why it is 20 MB and not "bigger than the biggest toy model".
_MIN_MODEL_BYTES = 20 << 20

# A .gguf that is not a model: a vision projector, a draft/MTP head, or imatrix
# calibration data. All three load *into* a model; none of them is one, and each
# sits next to real models in the library looking exactly like one. `mmproj`/
# `mtp`/`draft` is the convention modeladd already uses for the same question —
# shared, so the two can never disagree about what the library contains.
# This used to derive its own pattern by rewriting _AUX_PREFIX_RE.pattern, which
# silently matched nothing (the source had `(mmproj|…)` without the `(?:`) and so
# returned the un-widened original. Two rules that only looked shared, both
# missing imatrix-*. Import the one opinion instead of re-deriving it, so there is
# nothing left to drift.
_NOT_A_MODEL = _AUX_PREFIX_RE

# `name-00001-of-00003.gguf`: one file of one model. llama.cpp reads the header
# of the first part, which describes the *whole* model — so the cost of a shard
# is the size of its set, never its own.
_SHARD = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<n>\d{5})\.gguf$", re.I)

# How llama.cpp says "I ran out of memory". Distinct from "this binary is
# broken": the first is the machine's fault and must not be blamed on a build.
_OOM = re.compile(
    r"failed to allocate|unable to allocate|out of memory|bad_alloc"
    r"|not enough memory|failed to mmap|could not lock memory"
    r"|resource temporarily unavailable", re.I)


def _sz(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _indexed_candidates(avail: int, log) -> list[tuple[Path, int, str]]:
    """Candidates from the model index (§4.9 N2), ordered by what the model
    actually costs rather than the size of its files.

    The difference is the KV cache. A file-size gate asks whether the weights fit;
    a worker also allocates a cache, and on a hybrid model at full context that is
    a few GiB, which is exactly the margin a smoke test runs with. N1 measured
    gemma-4-31B at 16.4 GiB of files and 27.5 GiB of total — a gate on the first
    number picks a model that dies allocating cache, and the 4096-token clamp the
    owner diagnosed is the same class of failure: enough for weights, not for
    caches.

    Returns [] when the index is empty or unreadable, so the caller falls back to
    walking the directory. The index is an optimization here, not a dependency: a
    stale or absent index must not be able to block an update.
    """
    # The whole body is guarded, not just the query: rows come out of portal.db,
    # and a row with a null path or a non-numeric byte count is a malformed-row
    # problem, not a reason to fail a build. The contract is "index or walk", so
    # anything that is not an answer is treated as the absence of one.
    try:
        need = avail - _SMOKE_HEADROOM
        if need <= 0:
            return []
        rows = modelindex.fits_query(need, limit=_SMOKE_ATTEMPTS * 3)
        out: list[tuple[Path, int, str]] = []
        for r in rows:
            path = r.get("path")
            size = _as_size(r.get("bytes"))
            if not path or size is None:
                continue          # unusable row: skip, do not guess
            p = Path(path)
            if not p.is_file():
                continue          # index is newer than the disk
            parts = r.get("shard_total") or 1
            label = f"{r.get('id') or p.stem} (indexed)"
            if parts > 1:
                label = f"{r.get('id') or p.stem} ({parts} parts, indexed)"
            out.append((p, size, label))
        return out
    except Exception as e:  # noqa: BLE001 — any index problem means: walk instead
        log(f"smoke worker: model index unavailable ({type(e).__name__}: {e}) "
            f"— falling back to scanning the directory")
        return []


def _as_size(v) -> int | None:
    """A byte count from an index row, or None if it is not one.

    int() raises on a string like "87G" and on None; both are plausible in a row
    written by a different version of the indexer, and neither should be able to
    take down candidate selection.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and math.isfinite(v) and v >= 0:
        return math.floor(v)
    return None


def _model_candidates(models_dir: Path) -> list[tuple[Path, int, str]]:
    """Loadable models on disk, smallest first, as (path to load, bytes, label).

    Shards collapse into their first part at the size of the whole set; a set
    with a part missing is not loadable and is left out entirely. Projectors and
    imatrix files are not models. Zero-byte and truncated files are left out
    because a partially-downloaded model fails a smoke test for the wrong reason.
    """
    if not models_dir.is_dir():
        return []
    singles: list[tuple[Path, int, str]] = []
    groups: dict[tuple[str, str], dict[str, tuple[Path, int]]] = {}
    for f in models_dir.rglob("*.gguf"):
        if not f.is_file() or _NOT_A_MODEL.search(f.name):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        m = _SHARD.match(f.name)
        if m:
            groups.setdefault((str(f.parent), m["base"]), {})[m["idx"]] = (f, size)
            continue
        if size < _MIN_MODEL_BYTES:
            continue
        singles.append((f, size, f.stem))
    for (parent, base), parts in groups.items():
        want = int(next(iter(parts.values()))[0].name.rsplit("-of-", 1)[1]
                   .split(".")[0])
        if len(parts) < want or "00001" not in parts:
            continue      # incomplete set: llama.cpp cannot load it
        total = sum(v[1] for v in parts.values())
        if total < _MIN_MODEL_BYTES:
            continue
        singles.append((parts["00001"][0], total, f"{base} ({want} parts)"))
    return sorted(singles, key=lambda c: c[1])


def _mem_available() -> int:
    """Bytes the kernel says are available (cache reclaim counted, buffers not)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):  # non-Linux, or a kernel that won't say
        pass
    return 8 << 30   # assume a modest box rather than skipping the gate


def _is_memory_error(out: str) -> bool:
    return bool(_OOM.search(out))


def _run_worker(rel_dir: Path, model: Path, log, label: str, size: int) -> tuple[bool, str]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    log(f"smoke worker: {label} ({_sz(size)}) on 127.0.0.1:{port}")
    proc = subprocess.Popen(
            [str(rel_dir / "llama-server"), "--model", str(model),
             "--host", "127.0.0.1", "--port", str(port), "-c", "128"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # drain the pipe in a thread: a chatty startup can fill the 64 KB
    # buffer and deadlock the server before it ever binds the port
    tail = deque(maxlen=60)

    def _drain() -> None:
        assert proc.stdout is not None
        for ln in proc.stdout:
            tail.append(ln)
        proc.stdout.close()

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    ok = False
    try:
        for _ in range(180):
            if proc.poll() is not None:
                break
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if r.status_code == 200:
                    ok = True
                    break
            except Exception:  # noqa: BLE001 — not up yet
                time.sleep(1)
            else:
                time.sleep(1)  # server up but /health not 200 yet
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        drainer.join(timeout=5)
    return ok, "".join(tail)


# ------------------------------------------------------------------ helpers

_VER_RE = re.compile(r"build (\d+), commit ([0-9a-f]{7,40})")


def _version_lines(output: str) -> list[str]:
    """Lines of a `llama-server --version` run that actually look like version info.

    Do not take the first line of the output. Upstream changed what --version prints:
    b11028 printed only the version, so `splitlines()[0]` worked. b11056 emits a
    logger line first —

        0.00.000.734 I srv  llama_server: initializing ...
        version: 0.4.1-dev (build 11056, commit e613ef2c8)
        built with GNU 15.2.0 for Linux x86_64

    — and line 0 is that log line. The two call sites that *gate* on the result
    (the post-build identity check and the staged-release verification) then failed
    with "unexpected --version output", killing an update job after a 10-20 minute
    build had already succeeded. A probe that breaks because the tool it probes got
    a new first line is a probe reading position instead of content.

    Filtering is by the shape of a version line, and the filter is deliberately not
    `_VER_RE` itself: the human-facing sites (the binary's reported version, the
    purge record) want the version text even when the build/commit pair is absent,
    e.g. a release build or a different upstream format.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    keep = [ln for ln in lines if _VERSIONISH_RE.search(ln)]
    # Nothing matched: return everything rather than []. Callers decide whether an
    # unrecognised version string is fatal; silently returning [] would turn "I could
    # not read this" into "this binary reported nothing", which reads as a broken
    # binary rather than an unreadable one.
    return keep or lines


_VERSIONISH_RE = re.compile(
    r"\bversion:\s|\bbuild \d+|\bbuilt with \S|\bllama\.cpp\b|\bcommit [0-9a-f]{7,}",
    re.IGNORECASE)


def _link_target(link: Path) -> Path | None:
    """Where a symlink points, or None if it is dangling or unreadable."""
    try:
        return link.resolve(strict=True)
    except OSError:
        return None


def _points_into(target: Path | None, directory: Path) -> bool:
    return bool(target) and (target == directory or directory in target.parents)


# A release directory the portal built looks like this, and only this. It is the
# boundary of what the purge will touch, and it has to be: the registry cannot be
# the authority on a build it never heard of — that is precisely the situation
# being cleaned up — so the *shape* of the name plus containment under
# ~/ai/releases carries the constraint instead.
_REL_DIR_RE = re.compile(r"^llama\.cpp-b(\d+)$")


def _procs_using(directory: Path) -> list[int] | None:
    """pids executing from `directory`; None if the kernel will not say.

    `/proc/<pid>/exe` is the only source that does not depend on the registry
    being right, which is the thing in question here. None means unverifiable,
    and callers must treat that as *not* deletable.

    Honest limitation: a process owned by someone else has an unreadable exe and
    is invisible to this. Every service here runs as this user, so the blind spot
    is a root process started by hand — which is also the case the farm check
    would miss, so both are reported as "not verified" rather than "safe".
    """
    procs = Path("/proc")
    if not procs.is_dir():
        return None
    out: list[int] = []
    try:
        entries = list(procs.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{entry.name}/exe")
        except OSError:  # exited between listing and readlink, or not ours
            continue
        # a binary unlinked while running reads back as "…/llama-server (deleted)"
        exe = exe.removesuffix(" (deleted)")
        if _points_into(Path(exe), directory):
            out.append(int(entry.name))
    return sorted(out)


def _farm_links_into(directory: Path) -> list[dict]:
    """Which `~/ai/bin` links *name* a path inside `directory`.

    Read lexically (readlink + normpath) rather than by resolving the symlink,
    because the farm is a configuration and a broken link is still a declaration:
    ``~/ai/bin/llama-server -> ../releases/llama.cpp-b1043/llama-server`` says
    that build is the one the router runs, even while the target is missing. If
    the directory is deleted behind it, the declaration becomes unrecoverable and
    the reason it existed is gone. So dangling links hold too, and are labelled
    as such — a broken link is a different problem to explain, not a licence to
    delete.
    """
    out: list[dict] = []
    if not paths.AI_BIN.is_dir():
        return out
    for l in sorted(paths.AI_BIN.glob("*")):
        if not l.is_symlink():
            continue
        try:
            raw = Path(os.readlink(l))
        except OSError:  # vanished between listing and reading
            continue
        target = raw if raw.is_absolute() else l.parent / raw
        norm = Path(os.path.normpath(str(target)))
        if _points_into(norm, directory):
            out.append({"name": l.name, "dangling": not l.exists()})
    return out


def _farm_note(held: list[dict]) -> str:
    """`llama-server, llama-cli (broken link)` — naming a broken link is the
    actionable half: the fix is the link, not necessarily the build."""
    return ", ".join(x["name"] + (" (broken link)" if x["dangling"] else "")
                     for x in held) or "(unnamed link)"


def stray_releases() -> dict:
    """Build directories on disk that the registry has never heard of.

    An update that dies between staging and recording — interrupted by a reboot,
    or failed by a smoke test that asked a 94 GB model for 37 GB of memory —
    leaves a complete ~400 MB release directory that no list shows, so the user
    cannot see it and has no way to remove it. It also has to be found
    separately from `delete_release`, whose first guard is "is it registered",
    which is the one thing that cannot be true here.

    Every entry is reported with the two facts that make deletion dangerous — the
    bin farm still pointing into it, or a process executing from it — and
    `purgeable` is False unless both are clear and /proc could be read at all.
    """
    root = paths.RELEASES
    out = {"strays": [], "unexpected": [], "bytes": 0, "scanned": bool(root.is_dir())}
    if not root.is_dir():
        return out
    reg = releases()
    known = {r.get("name") for r in reg.get("releases", [])}
    current = reg.get("current")
    try:
        cur_build = int(_REL_DIR_RE.match(current or "").group(1))
    except (AttributeError, ValueError):
        cur_build = 0

    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in known:
            continue
        m = _REL_DIR_RE.match(d.name)
        if not m:                       # not ours; report, never offer to delete
            out["unexpected"].append({"name": d.name, "path": str(d)})
            continue
        held = _farm_links_into(d)
        pids = _procs_using(d)
        why = None
        if held:
            why = ("~/ai/bin still links into it (" + _farm_note(held)
                   + ") — this build is what the router is configured to run")
        elif pids is None:
            why = "cannot check for running processes (no readable /proc)"
        elif pids:
            why = f"a process is running from it (pid {', '.join(map(str, pids))})"
        size = release_size(d) or 0
        out["strays"].append({
            "name": d.name, "path": str(d), "bytes": size,
            "build": int(m.group(1)),
            "ahead": bool(cur_build and int(m.group(1)) > cur_build),
            "built_dir": datetime.fromtimestamp(d.stat().st_mtime,
                                                timezone.utc).isoformat(),
            "held_by": [x["name"] for x in held],
            "pids": pids or [], "proc_read": pids is not None,
            "purgeable": why is None, "why_not": why,
        })
        out["bytes"] += size
    out["strays"].sort(key=lambda s: -s["bytes"])
    return out


# Release directories are immutable once built, so their size is worth caching:
# the dashboard polls the release list and a size walk over a dozen builds on a
# busy disk should not ride along with every poll.
_SIZE_CACHE: dict[str, tuple[float, int | None]] = {}
_SIZE_TTL = 300.0


def release_size(path, force: bool = False) -> int | None:
    """Bytes under a release directory (recursive), or None if it is missing."""
    if not path:
        return None
    p = Path(path)
    if not p.is_dir():
        return None
    key = str(p)
    now = time.time()
    hit = _SIZE_CACHE.get(key)
    if hit and not force and now - hit[0] < _SIZE_TTL:
        return hit[1]
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(p):
            for f in files:
                try:
                    fp = Path(dirpath) / f
                    if not fp.is_symlink():
                        total += fp.stat().st_size
                except OSError:
                    continue    # raced with a build; the next refresh sees it
    except OSError:
        return None
    _SIZE_CACHE[key] = (now, total)
    return total


def releases() -> dict:
    """The release registry (~/ai/state/llama-releases.json); {}-ish if absent."""
    try:
        return json.loads(paths.RELEASES_JSON.read_text())
    except (OSError, ValueError):
        return {"current": None, "releases": []}


def _hipcc_shim_env(env: dict, log) -> dict:
    """hipcc shim: ROCm 10.1's clang headers omit four scalar f32 ocml prototypes
    that hip_bf16.h calls — every ggml-cuda TU dies on gfx1151 (6-line repro).
    The shim file restores the declarations; the file gates the shim — absent,
    the build env is unmodified. Proven sound: compiles, links, runs log2(8)=3.0.
    """
    shim = paths.HIP_OCML_SHIM
    if shim.is_file():
        prev = env.get("HIPCC_COMPILE_FLAGS_APPEND", "")
        env["HIPCC_COMPILE_FLAGS_APPEND"] = \
            f"-include {shim}" + (f" {prev}" if prev else "")
        log(f"hipcc shim active: {shim}")
    return env


def _run(log, cmd: list, timeout: int = 1800, **kw) -> str:
    """Echo + run + capture; raises RuntimeError with the tail of the output."""
    log("$ " + " ".join(str(c) for c in cmd))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       timeout=timeout, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): "
                           f"{' '.join(str(c) for c in cmd)}\n"
                           f"{(p.stdout or '')[-2000:]}{(p.stderr or '')[-2000:]}")
    return (p.stdout or "") + (p.stderr or "")


def _git(repo: Path, *args: str, log=print, **kw) -> str:
    return _run(log, ["git", "-C", str(repo), *args], **kw)


def _router_healthy(log, wait: int = 90) -> bool:
    """Wait until the router unit is active AND answering on its configured
    endpoint (``/v1/models``, with the API key when one is configured)."""
    deadline = time.time() + wait
    url = f"{serviceconf.base_url('llamacpp', 8080)}/v1/models"
    while time.time() < deadline:
        st = systemd.unit_state(paths.ROUTER_UNIT)
        if st.get("ActiveState") == "active":
            try:
                r = httpx.get(url, timeout=5, headers=llamakey.request_headers())
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 — still coming up
                pass
        time.sleep(3)
    log(f"router did not become healthy in time ({url})")
    return False


def verify_wrapper_argv(log=print) -> dict:
    """Run the generated wrapper with ``AI_PORTAL_PRINT_ARGV=1``.

    This is the safety net for the launch-config change: it proves the wrapper
    is syntactically valid, the binary is executable, and the argv matches what
    the portal computes — without loading a model or touching the running
    router. ``ok`` is False when the wrapper is missing or disagrees.
    """
    wrapper = units.wrapper_path("llamacpp")
    if not wrapper.is_file():
        return {"ok": False, "error": f"wrapper not installed: {wrapper}"}
    want = router_argv()
    try:
        p = subprocess.run([str(wrapper)], capture_output=True, text=True,
                           timeout=20,
                           env={**os.environ, "AI_PORTAL_PRINT_ARGV": "1"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "wrapper": str(wrapper)}
    if p.returncode != 0:
        return {"ok": False, "error": ((p.stderr or p.stdout).strip()[:400]
                                        or f"exit {p.returncode}"),
                "wrapper": str(wrapper)}
    got = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return {"ok": got == want, "wrapper": str(wrapper), "argv": got,
            "expected": want,
            "mismatch": None if got == want else "wrapper argv != computed argv"}
