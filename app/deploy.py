"""Deploy / update the portal as a user service running from ``~/ai``.

The repo is where code is *written*; ``~/ai/ai-portal`` is where it *runs*
(PORTAL_PLAN §0/§J). Keeping them apart means an update never touches a tree
somebody is editing, and a bad deploy is one command to undo.

    python -m app.deploy                 # deploy current HEAD to ~/ai/ai-portal
    python -m app.deploy --dry-run       # exactly what would happen
    python -m app.deploy --ref v0.2.0    # deploy a specific commit/tag
    python -m app.deploy --rollback      # redeploy the previously recorded sha
    python -m app.deploy --no-start      # install + enable, don't start

Mechanics, in order:

1. **Export** the chosen git ref (``git archive``) into the target — never the
   working tree, so what runs is always reproducible from a sha. A dirty tree
   warns instead of silently shipping edits.
2. **Venv** in the target: ``uv sync --frozen`` when uv is available (lockfile
   decides versions), else ``python -m venv`` + ``pip install -e .``.
3. **Launch files**, all generated (:mod:`app.core.units`): ``portal.env``,
   ``~/ai/bin/ai-portal.sh``, ``~/.config/systemd/user/ai-portal.service``
   (an existing unit file is backed up to ``state/rollback/units/`` first).
4. **Record** ``state/portal/deployment.json`` with the sha, target, python and
   the previous deployment — that record is what ``--rollback`` reads.

Nothing here rewrites the other services' units; and because the portal unit's
``ExecStart`` is the wrapper, a later bind/port change needs no daemon-reload.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import paths, serviceconf, units

TARGET_DEFAULT = paths.AI_ROOT / "ai-portal"
DEPLOYMENT_JSON = paths.PORTAL_STATE / "deployment.json"
UNIT_NAME = "ai-portal.service"     # systemd unit file name
PORTAL_SERVICE_NAME = "portal"        # env file + wrapper name
# Required in whatever the ref exports (git archive exports the whole tree —
# simpler and more honest than cherry-picking directories, and the deployed
# tree can then run its own tests).
REQUIRED_AT_TARGET = ("app", "web", "pyproject.toml")


# --------------------------------------------------------------------- utils
def _run(cmd: list[str], dry: bool = False, cwd: Path | None = None,
         capture: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    if cwd:
        printable = f"(cd {cwd}) && {printable}"
    if dry:
        print(f"$ {printable}   # dry-run")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    print(f"$ {printable}")
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                          capture_output=capture, text=True, timeout=timeout)


def git(*args: str, cwd: Path | None = None) -> str:
    """git in this repo (the source of the deploy)."""
    root = Path(__file__).resolve().parent.parent
    p = subprocess.run(["git", *args], cwd=str(cwd or root),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(p.stderr or '').strip()}")
    return p.stdout.strip()


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel"))


def resolve_ref(ref: str = "HEAD") -> str:
    """Full sha for a ref (and a clear error if the ref does not exist)."""
    return git("rev-parse", "--verify", f"{ref}^{{commit}}")


def dirty() -> list[str]:
    return [l for l in git("status", "--porcelain").splitlines() if l.strip()]


# ------------------------------------------------------------------ planning
def plan(target: Path, ref: str = "HEAD") -> dict:
    """What a deploy would do — computed without touching anything."""
    sha = resolve_ref(ref)
    prev = read_deployment()
    return {
        "target": str(target),
        "source_repo": str(repo_root()),
        "ref": ref,
        "sha": sha,
        "current_sha": (prev or {}).get("sha"),
        "would_change": (prev or {}).get("sha") != sha,
        "first_deploy": not target.exists(),
        "dirty_tree": bool(dirty()),
        "unit": paths.PORTAL_UNIT,
        "env_file": str(units.env_path(PORTAL_SERVICE_NAME)),
        "wrapper": str(units.wrapper_path(PORTAL_SERVICE_NAME)),
        "venv": str(target / ".venv"),
    }


def read_deployment() -> dict | None:
    try:
        return json.loads(DEPLOYMENT_JSON.read_text())
    except (OSError, ValueError):
        return None


def write_deployment(entry: dict) -> dict:
    prev = read_deployment() or {}
    history = list(prev.get("history") or [])
    if prev.get("sha"):
        history = ([{"sha": prev["sha"], "target": prev.get("target"),
                     "deployed_at": prev.get("deployed_at")}] + history)[:10]
    entry = {**entry, "history": history}
    DEPLOYMENT_JSON.parent.mkdir(parents=True, exist_ok=True)
    DEPLOYMENT_JSON.write_text(json.dumps(entry, indent=2) + "\n")
    return entry


# ------------------------------------------------------------------- steps
def export_ref(sha: str, target: Path, dry: bool = False,
               repo: Path | None = None) -> None:
    """``git archive <sha> | tar -x`` into target (clean copy, no .git).

    Atomic-ish: everything lands in a sibling scratch dir first and is renamed
    into place only after the ref is verified to be a deployable ai-portal tree,
    so a failed export cannot leave the running deployment half-overwritten.
    """
    repo = Path(repo) if repo else repo_root()
    if dry:
        print(f"# dry-run: git archive {sha[:12]} | tar -x -> {target}")
        return
    # export to a scratch dir first: a failed extract must not leave the live
    # deployment half-overwritten
    tmp = target.parent / f".{target.name}.new-{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        archive = subprocess.run(["git", "-C", str(repo), "archive", sha],
                                 capture_output=True, timeout=300)
        if archive.returncode != 0:
            raise RuntimeError("git archive failed")
        tar = subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive.stdout,
                             capture_output=True, timeout=300)
        if tar.returncode != 0:
            raise RuntimeError(f"extract failed: {tar.stderr.decode()[:300]}")
        missing = [p for p in REQUIRED_AT_TARGET if not (tmp / p).exists()]
        if missing:
            raise RuntimeError(f"ref {sha[:12]} is missing {missing} "
                               f"— not a deployable ai-portal tree")
        # swap: rename the old one aside, move the new one in, keep old for a
        # second (the venv is recreated, so this is fast)
        old = target.parent / f".{target.name}.old-{int(time.time())}"
        if target.exists():
            target.rename(old)
        tmp.rename(target)
        shutil.rmtree(old, ignore_errors=True)
        print(f"  exported {sha[:12]} -> {target}")
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def make_venv(target: Path, dry: bool = False) -> str:
    """Create the runtime venv in the target; returns the python path."""
    uv = shutil.which("uv")
    if uv:
        p = _run([uv, "sync", "--frozen"], dry=dry, cwd=target, timeout=1800)
        if getattr(p, "returncode", 1) != 0:
            print(f"  uv sync failed, retrying without --frozen: "
                  f"{(getattr(p, 'stderr', '') or '')[-400:]}")
            p = _run([uv, "sync"], dry=dry, cwd=target, timeout=1800)
            if getattr(p, "returncode", 1) != 0:
                raise RuntimeError("uv sync failed — see output above")
    else:  # pragma: no cover - uv is the documented toolchain
        py = sys.executable
        _run([py, "-m", "venv", str(target / ".venv")], dry=dry)
        _run([str(target / ".venv/bin/python"), "-m", "pip", "install", "-e",
              str(target)], dry=dry)
    return str(target / ".venv/bin/python")


def write_build_file(target: Path, sha: str) -> Path | None:
    """Stamp the deployed tree with the commit it was built from.

    ``/healthz`` reports it, so "I deployed X" and "X is running" are the same
    checkable fact rather than two claims — the gap that let a deploy look
    successful while the previous process kept serving. Nothing is written in
    dry-run mode (the target may not exist yet).
    """
    if not target.is_dir():
        return None
    p = target / "BUILD.json"
    p.write_text(json.dumps({
        "sha": sha,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_repo": str(repo_root()),
    }, indent=2) + "\n")
    print(f"  stamped {p} ({sha[:12]})")
    return p


def write_launch_files(target: Path, python: str, dry: bool = False) -> dict:
    """portal.env + wrapper + unit, all generated (see app.core.units)."""
    conf = serviceconf.SERVICE_CONF["portal"]
    env = serviceconf.ensure_defaults("portal")
    env.update({
        "AI_PORTAL_DIR": str(target),
        "AI_PORTAL_PYTHON": python,
        # uvicorn's own log target is stdout (journal); the portal adds its
        # rotating file under ~/ai/logs via core.logsetup
        "AI_PORTAL_LOG_LEVEL": os.environ.get("AI_PORTAL_LOG_LEVEL", "info"),
    })
    if dry:
        print(f"# dry-run: write {conf['env_file']} ({len(env)} keys), "
              f"{units.wrapper_path(PORTAL_SERVICE_NAME)}, "
              f"{units.unit_path(paths.PORTAL_UNIT)}")
        return {"env": env, "dry_run": True}
    units.write_env(PORTAL_SERVICE_NAME, env, log=lambda m: print("  " + m))
    units.write_wrapper(PORTAL_SERVICE_NAME, render_portal_wrapper(env),
                        log=lambda m: print("  " + m))
    info = units.install_unit(paths.PORTAL_UNIT, PORTAL_SERVICE_NAME,
                              "AI Tool Management Portal (web console)",
                              str(target), log=lambda m: print("  " + m)) or {}
    return {"env": env, "wrapper": str(units.wrapper_path(PORTAL_SERVICE_NAME)),
            "unit_file": info.get("unit_file"), "unit_backup": info.get("backup")}


# @@-tokens (see ROUTER_WRAPPER in providers/llamacpp.py: the shell body is
# full of ${VARS} and printf '%s', which %- or {}-formatting would eat).
PORTAL_WRAPPER = """#!/bin/sh
# AI portal - portal launch wrapper (GENERATED, do not hand-edit).
# Parameters come from ~/ai/config/portal.env; edit via the Settings page or
# `python -m app.deploy`, then restart ai-portal.
set -eu
DIR="${AI_PORTAL_DIR:-@@dir@@}"
PY="${AI_PORTAL_PYTHON:-@@python@@}"
if [ ! -x "$PY" ]; then
  echo "ai-portal: no python at $PY (run: python -m app.deploy)" >&2
  exit 1
fi
if [ ! -f "$DIR/app/main.py" ]; then
  echo "ai-portal: no app tree at $DIR" >&2
  exit 1
fi
cd "$DIR"
exec "$PY" -m uvicorn app.main:app \\
  --host "${AI_PORTAL_HOST:-@@host@@}" \\
  --port "${AI_PORTAL_PORT:-@@port@@}" \\
  --log-level "${AI_PORTAL_UVICORN_LOG_LEVEL:-warning}"
"""


def render_portal_wrapper(env: dict[str, str]) -> str:
    conf = serviceconf.SERVICE_CONF["portal"]
    return (PORTAL_WRAPPER
            .replace("@@dir@@", env.get("AI_PORTAL_DIR", str(TARGET_DEFAULT)))
            .replace("@@python@@", env.get("AI_PORTAL_PYTHON", sys.executable))
            .replace("@@host@@", env.get(conf["host_key"], "0.0.0.0"))
            .replace("@@port@@", str(env.get(conf["port_key"], "8090"))))


def unit_status(unit: str) -> dict:
    from .core import systemd
    return systemd.unit_state(unit)


def print_units() -> int:
    """Render the launch files for every managed service (no writes).

    The units are *generated* (app.core.units) — this is how you inspect or
    refresh the committed reference copy in deploy/GENERATED_UNITS.txt, and how
    you diff what systemd should have against what the portal would write.
    """
    from .providers.comfyui import COMFY_WRAPPER
    from .providers.llamacpp import render_router_wrapper, router_env

    out: list[str] = ["# GENERATED — do not edit by hand",
                      "# source: app/core/units.py + the providers' ensure_service()",
                      "# refresh: python -m app.deploy --print-units > deploy/GENERATED_UNITS.txt",
                      "# note: paths reflect THIS machine (HOME / ~/ai); the units are written",
                      "#       with the real absolute paths when a service is installed.", ""]

    specs = [
        (paths.COMFY_UNIT, "comfyui", "ComfyUI (AI Portal)", paths.COMFY_REPO,
         COMFY_WRAPPER),
        (paths.ROUTER_UNIT, "llamacpp", "llama.cpp model router (AI Portal)",
         paths.AI_ROOT, render_router_wrapper(router_env())),
        (paths.PORTAL_UNIT, "portal", "AI Tool Management Portal (web console)",
         TARGET_DEFAULT,
         render_portal_wrapper({**serviceconf.ensure_defaults("portal"),
                                "AI_PORTAL_DIR": str(TARGET_DEFAULT),
                                "AI_PORTAL_PYTHON": str(
                                    (read_deployment() or {}).get(
                                        "python",
                                        TARGET_DEFAULT / ".venv/bin/python"))})),
    ]
    for unit, svc, desc, workdir, wrapper in specs:
        out += [f"===== {unit}.service =====\n"
                + units.render_unit(unit, svc, desc, workdir),
                f"===== {unit} wrapper ({units.wrapper_path(svc)}) =====\n{wrapper}"]
    text = "\n".join(x if x.endswith("\n") else x + "\n" for x in out)
    print(text)
    return 0


# --------------------------------------------------------------------- main
def deploy(target: Path = TARGET_DEFAULT, ref: str = "HEAD", dry: bool = False,
           start: bool = True, message: str = "") -> dict:
    p = plan(target, ref)
    print(f"deploy {p['ref']} ({p['sha'][:12]}) -> {p['target']}")
    if p["dirty_tree"] and ref.upper() == "HEAD":
        print("  note: source tree has uncommitted changes; git archive deploys "
              "the *commit*, so those changes are NOT included")
    if not p["would_change"] and not dry:
        print("  same sha as the running deployment — refreshing venv + launch "
              "files (use --ref to pick another commit)")

    export_ref(p["sha"], target, dry=dry)
    python = make_venv(target, dry=dry)
    write_build_file(target, p["sha"])
    launch = write_launch_files(target, python, dry=dry)

    if dry:
        return {**p, "dry_run": True, "python": python}

    record = write_deployment({
        "sha": p["sha"], "ref": p["ref"], "target": str(target),
        "python": python, "unit": paths.PORTAL_UNIT,
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "message": message, "previous_sha": p["current_sha"],
    })

    if start:
        started = start_service(p["sha"], dry=False)
    else:
        subprocess.run(["systemctl", "--user", "enable", UNIT_NAME],
                       capture_output=True, text=True)
        started = {"action": "enable-only"}
    st = unit_status(paths.PORTAL_UNIT)
    result = {**p, "python": python, "launch": launch, "state": st,
              "start": started, "record": str(DEPLOYMENT_JSON)}
    print(f"  unit {paths.PORTAL_UNIT}: {st.get('ActiveState')}/{st.get('SubState')}")
    print(f"  recorded in {DEPLOYMENT_JSON}")
    return result


def start_service(sha: str | None = None, dry: bool = False) -> dict:
    """Enable + (re)start the portal unit, then verify what is serving.

    ``enable --now`` on an *already active* unit is a no-op — which silently
    meant "deployed" while the old code kept serving. So: enable, then restart
    if active, start if not, and finally ask the running process (``/healthz``)
    which build it is, so the claim is checked instead of assumed.
    """
    if dry:
        print(f"# dry-run: systemctl --user enable {UNIT_NAME}; "
              f"restart-or-start; verify /healthz sha")
        return {"action": "dry-run"}
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME],
                   capture_output=True, text=True)
    was_active = unit_status(paths.PORTAL_UNIT).get("ActiveState") == "active"
    action = "restart" if was_active else "start"
    p = subprocess.run(["systemctl", "--user", action, UNIT_NAME],
                       capture_output=True, text=True, timeout=120)
    print(f"  systemctl --user {action} {paths.PORTAL_UNIT}"
          + ("" if p.returncode == 0 else f" FAILED: {(p.stderr or p.stdout).strip()[:200]}"))
    health = wait_for_health(sha)
    print(f"  healthz: {health}")
    return {"action": action, "health": health}


def wait_for_health(expected_sha: str | None = None, timeout: float = 30.0) -> dict:
    """Poll /healthz until it answers (and, if given, reports the build sha).

    Uses urllib: this module must stay stdlib-only so it runs from a fresh
    clone with no venv."""
    import json as _json
    import urllib.error
    import urllib.request

    base = serviceconf.base_url("portal", default_port=8090)
    deadline = time.time() + timeout
    last: dict = {"ok": False, "error": "no response"}
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=3) as r:
                body = _json.loads(r.read().decode() or "{}")
            served = body.get("build") or {}
            last = {"ok": True, "url": f"{base}/healthz",
                    "sha": (served.get("sha") or "?")[:12],
                    "app_version": body.get("app_version")}
            if expected_sha:
                last["matches_deployed"] = (served.get("sha") == expected_sha)
                if last["matches_deployed"]:
                    return last
            else:
                return last
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = {"ok": False, "error": str(e)[:120]}
        time.sleep(1.5)
    if expected_sha:
        last.setdefault("matches_deployed", False)
    return last


def rollback(dry: bool = False) -> dict:
    prev = read_deployment()
    if not prev:
        raise SystemExit("no deployment.json — nothing deployed by this script yet")
    hist = prev.get("history") or []
    if not hist:
        raise SystemExit("deployment history is empty — cannot roll back")
    ref = hist[0]["sha"]
    print(f"rolling back: {prev.get('sha', '')[:12]} -> {ref[:12]}")
    return deploy(ref=ref, message=f"rollback to {ref[:12]}", dry=dry)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.deploy",
        description="Deploy/update the AI Portal service under ~/ai")
    ap.add_argument("--target", default=str(TARGET_DEFAULT),
                    help=f"deployment dir (default {TARGET_DEFAULT})")
    ap.add_argument("--ref", default="HEAD",
                    help="git ref to deploy (default HEAD)")
    ap.add_argument("--dry-run", action="store_true", help="print the actions")
    ap.add_argument("--no-start", action="store_true",
                    help="install + enable but do not start the service")
    ap.add_argument("--rollback", action="store_true",
                    help="redeploy the previously recorded sha")
    ap.add_argument("--status", action="store_true",
                    help="show the recorded deployment + live unit state")
    ap.add_argument("--print-units", action="store_true",
                    help="render every generated unit/wrapper and exit")
    a = ap.parse_args(argv)

    if a.print_units:
        return print_units()

    if a.status:
        rec = read_deployment() or {}
        print(json.dumps({"recorded": rec,
                          "unit": unit_status(paths.PORTAL_UNIT),
                          "expect": (Path(rec.get("target", "?")) / "app" / "main.py").is_file()
                          if rec.get("target") else False}, indent=2))
        return 0
    if a.rollback:
        rollback(dry=a.dry_run)
        return 0
    deploy(target=Path(a.target).expanduser(), ref=a.ref, dry=a.dry_run,
           start=not a.no_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
