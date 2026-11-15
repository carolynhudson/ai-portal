"""The owner-gated push gate.

Mesh law: pushes to GitHub are owner-approved. This module is that
approval made mechanical: the operator presses the button on /push, the
browser confirm plus the body's ``confirm`` flag are the approval, and
no agent-facing route calls this. The portal commits + pushes ONLY when
a human said so.

Transport is whatever ``origin`` is configured to (SSH agent today; a
hub-held PAT over HTTPS via GIT_ASKPASS is the parked lane -- github.com
API proved unreachable from this box historically, HTTPS web proved
live 2026-11-14). The gate never force-pushes, never pushes a non-main
branch, and refuses when behind (the "pull first" law).

Redaction law: token material never rides logs, events, or responses --
message input is screened for common token shapes and screened-out.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REMOTE = "origin"
ALLOWED_BRANCH = "main"
_T = 25  # seconds per git call; the network calls get this, not forever


def _is_git(p: Path) -> bool:
    return (p / ".git").exists()


def resolve_repo() -> Path:
    """Which checkout the gate acts on. The running tree (~/ai/ai-portal)
    is a git-less export by design, so the answer is layered:
    explicit env, then the code's own tree if it is a checkout (dev
    runs), then the deploy record's source_repo (the repo code was
    written in), and failing all, the code tree -- where status() says
    so honestly."""
    env = os.environ.get("AI_PORTAL_REPO")
    if env and _is_git(Path(env)):
        return Path(env)
    if _is_git(REPO):
        return REPO
    try:
        from . import paths
        rec = json.loads((paths.PORTAL_STATE / "deployment.json").read_text())
        src = rec.get("source_repo", "")
        if src and _is_git(Path(src)):
            return Path(src)
    except (OSError, ValueError):
        pass
    return REPO

# Common GitHub token shapes (classic PAT, fine-grained, GitHub App).
_TOKEN_RE = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[srouo]_[A-Za-z0-9]{20,})")


def redact(text: str) -> str:
    """Strip token-shaped secrets from anything this module returns."""
    return _TOKEN_RE.sub("***redacted***", str(text))


def _git(args: list[str], repo: Path | str = REPO) -> tuple[int, str, str]:
    """Run git, total: (rc, out, err), never raises."""
    try:
        p = subprocess.run(["git"] + args, cwd=str(repo),
                           capture_output=True, timeout=_T, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)}: timeout"
    except OSError as exc:
        return 127, "", f"git: {exc}"


def _lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if l.strip()]


def status(repo: Path | str | None = None) -> dict:
    """A read of the repo's push state. Total: every field degrades to a
    reason string; nothing raises, the page must render honesty."""
    repo = Path(repo) if repo else resolve_repo()
    rc, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if rc != 0:
        return {"ok": False, "why": f"not a git checkout? {redact(branch)}"}
    rc, url, _ = _git(["remote", "get-url", REMOTE], repo)
    remote_url = redact(url) if rc == 0 else ""
    rc, ahead, _ = _git(["rev-list", "--count", f"{REMOTE}/{ALLOWED_BRANCH}..HEAD"], repo)
    rc2, behind, _ = _git(["rev-list", "--count", f"HEAD..{REMOTE}/{ALLOWED_BRANCH}"], repo)
    rc3, porcelain, _ = _git(["status", "--porcelain"], repo)
    rc4, log, _ = _git(["log", "--format=%h %s", f"{REMOTE}/{ALLOWED_BRANCH}..HEAD",
                        "-n", "50"], repo)
    # live remote check: the remote's own default branch (never assumed)
    rc5, sym, err5 = _git(["ls-remote", "--symref", REMOTE, "HEAD"], repo)
    remote_branch = ""
    if rc5 == 0:
        m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", sym)
        remote_branch = m.group(1) if m else ""
    dirty = len(_lines(porcelain)) if rc3 == 0 else -1
    return {
        "ok": True,
        "branch": branch,
        "remote_url": remote_url,
        "remote_branch": remote_branch,
        "remote_live": rc5 == 0,
        "remote_note": redact(err5)[:160] if rc5 != 0 else "",
        "ahead": int(ahead) if rc == 0 and ahead.isdigit() else -1,
        "behind": int(behind) if rc2 == 0 and behind.isdigit() else -1,
        "dirty": dirty,
        "commits": _lines(log) if rc4 == 0 else [],
    }


def push(message: str = "", repo: Path | str | None = None) -> dict:
    """The gated verb: stage, commit, push. Refusals are data, not traps.

    Called ONLY behind the confirm gate (route + browser confirm). The
    push itself is a fast-forward plain push: behind => refuse, non-main
    => refuse, remote default != main => refuse.
    """
    repo = Path(repo) if repo else resolve_repo()
    st = status(repo)
    if not st["ok"]:
        return {"ok": False, "why": st["why"], "log": []}
    log: list[str] = []
    if st["branch"] != ALLOWED_BRANCH:
        return {"ok": False, "why": f"on {st['branch']!r}; the gate pushes {ALLOWED_BRANCH} only",
                "log": log}
    msg = redact((message or "").strip())[:200]
    if not msg:
        msg = f"portal: owner-gated push {time.strftime('%Y-%m-%d %H:%M')}"
    if not st["remote_live"]:
        return {"ok": False, "why": f"remote unreachable: {st['remote_note']}", "log": log}
    if st["remote_branch"] and st["remote_branch"] != ALLOWED_BRANCH:
        return {"ok": False,
                "why": f"remote default is {st['remote_branch']!r}; the gate pushes {ALLOWED_BRANCH} only",
                "log": log}
    # settle against the remote before deciding (the fetch is the same one
    # the status made; do it again so the decision is current, not cached)
    rc, out, err = _git(["fetch", "--quiet", REMOTE], repo)
    if rc != 0:
        return {"ok": False, "why": f"fetch failed: {redact(err)[:160]}", "log": [f"fetch rc={rc}"]}
    rc, behind_s, _ = _git(["rev-list", "--count", f"HEAD..{REMOTE}/{ALLOWED_BRANCH}"], repo)
    behind = int(behind_s) if behind_s.isdigit() else -1
    if behind > 0:
        return {"ok": False, "why": f"behind remote by {behind}; pull/rebase first (no force-push lane)",
                "log": log}
    rc, out, err = _git(["add", "-A"], repo)
    log.append(f"add: {'ok' if rc == 0 else redact(err)[:120]}")
    if rc != 0:
        return {"ok": False, "why": "git add failed", "log": log}
    rc, out, err = _git(["commit", "-q", "-m", msg], repo)
    if rc == 0:
        log.append(f"commit: {msg}")
    elif "nothing to commit" in out + err:
        log.append("commit: nothing to commit (pushing existing)")
    else:
        return {"ok": False, "why": f"commit failed: {redact(err)[:160]}", "log": log}
    rc, out, err = _git(["push", REMOTE, f"HEAD:refs/heads/{ALLOWED_BRANCH}"], repo)
    tail = redact((out + "\n" + err).strip()).splitlines()[-4:] if (out + err).strip() else []
    log.extend(f"push: {l}" for l in tail)
    if rc != 0:
        return {"ok": False, "why": "push refused by remote (see log)", "log": log}
    _, head, _ = _git(["rev-parse", "--short", "HEAD"], repo)
    _, ahead_s, _ = _git(["rev-list", "--count", f"{REMOTE}/{ALLOWED_BRANCH}..HEAD"], repo)
    return {"ok": True, "head": head, "remaining_ahead": int(ahead_s) if ahead_s.isdigit() else -1,
            "log": log}
