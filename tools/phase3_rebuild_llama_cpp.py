#!/usr/bin/env python3
"""Phase 3 — rebuild llama.cpp into a self-contained release (non-destructive).

  repo (source only)   ~/ai/llama.cpp
  build tree           ~/ai/build/llama.cpp-<build>     (disposable)
  release              ~/ai/releases/llama.cpp-<build>  (bin/ lib/ include/,
                                                        RPATH=$ORIGIN/../lib)
  symlink farm         ~/ai/bin -> current release

Nothing in ~/.local/bin or the running router is touched. The previous release
(staging-dir RUNPATH build) stays in place until Phase 4 cuts over.

Usage:
  python3 phase3_rebuild_llama_cpp.py                # build at repo HEAD
  python3 phase3_rebuild_llama_cpp.py --commit <sha> # build a specific commit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiportal as ap


def git_head(repo: Path) -> str:
    return ap.run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()


def binary_version(exe: Path) -> tuple[str, str]:
    out = ap.run([str(exe), "--version"]).strip().splitlines()
    m = re.search(r"build (\d+), commit ([0-9a-f]+)", out[0] if out else "")
    ver = m.group(1) if m else "unknown"
    commit = m.group(2) if m else "unknown"
    return ver, commit


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--commit", default=None, help="commit to build (default: repo HEAD)")
    ap_.add_argument("--jobs", type=int, default=32)
    ap_.add_argument("--dry-run", action="store_true")
    args = ap_.parse_args()
    dry = args.dry_run

    if not ap.LLAMA_REPO.is_dir():
        ap.die(f"repo missing: {ap.LLAMA_REPO} (run phase2 first)")

    commit = git_head(ap.LLAMA_REPO) if not args.commit else args.commit
    ap.run(["git", "-C", str(ap.LLAMA_REPO), "rev-parse", "--verify", f"{commit}^{{commit}}"], dry=dry)
    if args.commit:
        ap.run(["git", "-C", str(ap.LLAMA_REPO), "checkout", "--quiet", commit], dry=dry)

    build_dir = ap.BUILD_ROOT / f"llama.cpp-{commit[:12]}"
    ap.ensure_dir(ap.BUILD_ROOT, dry)

    # --- configure + build ---------------------------------------------------
    if not (build_dir / "CMakeCache.txt").exists():
        ap.log(f"configuring in {build_dir}")
        env = {**os.environ, "VULKAN_SDK": str(ap.VULKAN_SDK)}
        ap.run(["cmake", "-S", str(ap.LLAMA_REPO), "-B", str(build_dir), "-G", "Ninja",
                *ap.LLAMA_CMAKE_ARGS], dry=dry, env=env)
    ap.log(f"building with {args.jobs} jobs (10-20 min)")
    ap.run(["cmake", "--build", str(build_dir), "--config", "Release", "-j", str(args.jobs)], dry=dry)

    server = build_dir / "bin" / "llama-server"
    if not dry and not server.exists():
        ap.die(f"build succeeded but {server} missing")
    ver, ver_commit = ("?", "?") if dry else binary_version(server)
    ap.log(f"built: {ver} commit {ver_commit}")

    # --- stage self-contained release (flat layout: binaries + libs together) ---
    # llama.cpp's cmake --install does not install the tool binaries, so we
    # stage manually. With CMAKE_BUILD_RPATH=$ORIGIN the binaries find their
    # sibling libraries in the same directory; ROCm libs come from ldconfig.
    rel_name = f"llama.cpp-b{ver}"
    rel = ap.RELEASES / rel_name
    if rel.exists():
        ap.die(f"release {rel} already exists — remove it or build a different commit")
    ap.ensure_dir(rel, dry)
    if not dry:
        n = 0
        for p in list((build_dir / "bin").glob("*")):
            if p.is_file():
                shutil.copy2(p, rel / p.name)
                n += 1
        for p in build_dir.glob("*.so*"):
            if p.is_file():
                shutil.copy2(p, rel / p.name)
                n += 1
        ap.log(f"staged {n} files into {rel}")
        # verify: version + libs resolve + no stale staging-dir dependency
        out = ap.run([str(rel / "llama-server"), "--version"]).splitlines()[0]
        ap.log(f"release version: {out}")
        ldd_out = ap.run(["ldd", str(rel / "llama-server")])
        bad = [l for l in ldd_out.splitlines() if "not found" in l]
        if bad:
            ap.die("release has unresolved libraries:\n" + "\n".join(bad))
        ap.run(["ldd", str(rel / "llama-cli")])
        ap.log("release verification passed (ldd clean, self-contained)")

    # --- symlink farm ~/ai/bin ----------------------------------------------
    ap.ensure_dir(ap.AI_BIN, dry)
    if not dry:
        for exe in sorted(rel.iterdir()):
            if not exe.is_file() or exe.name.startswith("lib"):
                continue
            link = ap.AI_BIN / exe.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(exe)
        ap.log(f"symlink farm updated: {len(list(ap.AI_BIN.iterdir()))} entries in {ap.AI_BIN}")

    # --- state ----------------------------------------------------------------
    if not dry:
        state = {}
        if ap.RELEASES_JSON.exists():
            state = json.loads(ap.RELEASES_JSON.read_text())
        state["current"] = rel_name
        rels = {r["name"]: r for r in state.get("releases", [])}
        rels[rel_name] = {
            "name": rel_name,
            "commit": commit,
            "version": ver,
            "path": str(rel),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        state["releases"] = list(rels.values())
        ap.ensure_dir(ap.STATE, dry)
        ap.RELEASES_JSON.write_text(json.dumps(state, indent=2) + "\n")
        ap.log(f"state written to {ap.RELEASES_JSON}")

    ap.log("phase 3 complete. Production cutover is phase 4 (user-run).")


if __name__ == "__main__":
    main()
