"""llama.cpp provider: release registry, deployed binary, router service, models."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .base import Provider, ToolStatus
from ..core import db, paths, systemd, updater


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

    # ------------------------------------------------------------------ C
    # Update pipeline (productionizes tools/phase3_rebuild_llama_cpp.py + the
    # phase-4 swap): fetch -> resolve ref -> build in ~/ai/build/ -> stage a
    # self-contained release in ~/ai/releases/ -> verify (version, ldd, smoke
    # worker) -> repoint the ~/ai/bin symlink farm -> restart the router.
    # The previous release stays on disk until a one-click rollback replaces
    # it again. See docs/PORTAL_PLAN.md §4.4.

    def update_status(self) -> dict:
        """Everything the dashboard's release bubble needs."""
        reg = releases()
        current = reg.get("current")
        entry = next((r for r in reg.get("releases", [])
                      if r.get("name") == current), None)
        return {
            "current": current,
            "current_commit": (entry or {}).get("commit"),
            "repo_head": _git_head(paths.LLAMA_REPO),
            "releases": reg.get("releases", []),
            "vulkan_sdk": str(paths.VULKAN_SDK),
            "job": updater.latest_job(self.key),
        }

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
        if not (build_dir / "CMakeCache.txt").exists():
            log(f"configuring in {build_dir} (SDK {paths.VULKAN_SDK.name})")
            _run(log, ["cmake", "-S", str(repo), "-B", str(build_dir),
                       "-G", "Ninja", *paths.LLAMA_CMAKE_ARGS], env=env)
        log(f"building (this is the slow step, ~10-20 min, "
            f"{os.cpu_count()} jobs)")
        _run(log, ["cmake", "--build", str(build_dir),
                   "-j", str(os.cpu_count() or 4)])
        server = build_dir / "bin" / "llama-server"
        if not server.exists():
            raise RuntimeError(f"build finished but {server} is missing")
        ver_line = _run(log, [str(server), "--version"]).splitlines()[0]
        m = _VER_RE.search(ver_line)
        if not m:
            raise RuntimeError(f"unexpected --version output: {ver_line}")
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
        staged = _run(log, [str(rel_dir / "llama-server"), "--version"]).splitlines()[0]
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
                    if ln.startswith("ref:") and "refs/heads/" in ln:
                        t = "origin/" + ln.split("refs/heads/", 1)[1].strip()
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
        """Boot a throwaway worker from the staged release loading the
        smallest model on disk — proves the release actually runs here."""
        model = None
        if paths.LLAMA_MODELS.is_dir():
            ggufs = [f for f in paths.LLAMA_MODELS.rglob("*.gguf") if f.is_file()]
            if ggufs:
                model = min(ggufs, key=lambda f: f.stat().st_size)
        if model is None:
            log("no models on disk — skipping smoke worker")
            return
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        log(f"smoke worker: {model.name} on 127.0.0.1:{port}")
        proc = subprocess.Popen(
            [str(rel_dir / "llama-server"), "--model", str(model),
             "--host", "127.0.0.1", "--port", str(port), "-c", "128", "-v", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if not ok:
            raise RuntimeError("smoke worker did not become healthy:\n"
                               + (proc.stdout or "")[-1500:])
        log("smoke worker OK")


# ------------------------------------------------------------------ helpers

_VER_RE = re.compile(r"build (\d+), commit ([0-9a-f]{7,40})")


def releases() -> dict:
    """The release registry (~/ai/state/llama-releases.json); {}-ish if absent."""
    try:
        return json.loads(paths.RELEASES_JSON.read_text())
    except (OSError, ValueError):
        return {"current": None, "releases": []}


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
    """Wait until the router unit is active AND answering on :8080."""
    deadline = time.time() + wait
    while time.time() < deadline:
        st = systemd.unit_state(paths.ROUTER_UNIT)
        if st.get("ActiveState") == "active":
            try:
                r = httpx.get(
                    f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}/v1/models",
                    timeout=5)
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 — still coming up
                pass
        time.sleep(3)
    log("router did not become healthy in time")
    return False
