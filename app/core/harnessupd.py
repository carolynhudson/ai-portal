"""The opencode harness's update state — read-only, Status page tab.

Three versions answer "is the harness current", and they are three genuinely
different facts (measured Oct-25):

* **running** — the version the live host answers with (`/api/info`). The systemd
  host keeps the binary it started with.
* **disk** — the version of `~/.opencode/bin/opencode` on disk. The CLI
  self-updates (autoupdate is opencode's default), so the disk moves under the
  running service: measured 2.0.32 running under a 2.1.4 disk.
* **latest** — the highest `v2` tag on the GitHub releases page. `releases/latest`
  is NOT the v2 channel (it answers v1.19.4, the v1 line, measured) — tags are
  the only honest channel, and the first page is enough (measured: v2 tags
  dominate the newest 100).

Plugins: the host reports what it LOADED (`POST /api/plugin/check`, measured),
the seed list is what will load next start (`harnessctl.plugin_specs()`), and
npm's dist-tags say what is available. An update is a PIN CHANGE + restart:
the seed writes pinned packages, restart pulls them.

No verb lives here. Reads are cheap and public (GitHub, npm, loopback host);
every write belongs to a confirm-gated route in main.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request

from . import harnessctl, logsetup

log = logsetup.get("harnessupd")

GH_TAGS_URL = "https://api.github.com/repos/anomalyco/opencode/tags?per_page=100"
NPM_URL = "https://registry.npmjs.org/"
_CACHE: dict = {"at": 0.0, "data": None}
TTL = 3600.0   # one check per hour answers the tab; GitHub's unauthenticated budget
_NET_TIMEOUT = 10.0


def _get_json(url: str, timeout: float = _NET_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "user-agent": "ai-portal update check",
        "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - https, fixed hosts
        return json.loads(r.read().decode())


def parse_ver(s) -> tuple | None:
    """`v2.1.4` / `2.1.4` / `opencode v2.1.4` -> (2, 1, 4); None when there are
    no three-part numbers — None never compares as zero, absence is not "old"."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(s))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def newer(a, b) -> bool:
    """True only when both sides carry a version and a > b."""
    va, vb = parse_ver(a), parse_ver(b)
    return bool(va and vb and va > vb)


def latest_v2() -> tuple[str | None, str | None]:
    """(version, note). Highest v2 tag on the first page; a note when the read
    said nothing usable. v1 tags are NOT newer than v2 and are excluded."""
    try:
        tags = _get_json(GH_TAGS_URL)
    except Exception as e:  # noqa: BLE001 - the page must state the failed read
        return None, f"the version read failed ({type(e).__name__})"
    vers = [t.get("name", "") for t in tags if t.get("name", "").startswith("v2")]
    if not vers:
        return None, "no v2 tags on the first release page (channel moved?)"
    best = max(vers, key=lambda v: parse_ver(v) or (0, 0, 0))
    return best.lstrip("v"), None


def disk_version() -> tuple[str | None, str | None]:
    """The on-disk binary's version (`<bin> --version`). Absent binary is a
    stated absence, not version zero."""
    binp = harnessctl.harness_bin()
    if not binp.exists():
        return None, f"no binary at {binp}"
    try:
        p = subprocess.run([str(binp), "--version"], capture_output=True,
                           text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return None, f"the version probe failed ({type(e).__name__})"
    if p.returncode != 0:
        return None, f"the version probe exited {p.returncode}"
    m = re.search(r"(\d+\.\d+\.\d+)", p.stdout + p.stderr)
    return (m.group(1) if m else None), (None if m else "the binary printed no version")


def loaded_plugins(specs: list[str]) -> tuple[dict, str | None]:
    """{name: {"version","status"}} from the live host's plugin check, plus a
    note when the host refused. The check reports builtins too; only the
    configured packages are kept."""
    want = {s.split("@")[0]: s for s in specs}
    try:
        _st, out = harnessctl._request("POST", "/api/plugin/check",
                                       body={"plugins": [{"package": s} for s in specs]})
    except harnessctl.HarnessError as e:
        return {}, str(e)
    rows = out.get("data") if isinstance(out, dict) else None
    found: dict = {}
    for row in rows or []:
        src = row.get("source") or {}
        if src.get("type") != "package":
            continue
        name = str(row.get("id") or "")
        if name in want:
            found[name] = {"version": src.get("version"),
                           "status": (row.get("state") or {}).get("status")}
    return found, None


def npm_latest(pkg: str) -> tuple[str | None, str | None]:
    try:
        d = _get_json(NPM_URL + pkg)
        return (d.get("dist-tags") or {}).get("latest"), None
    except Exception as e:  # noqa: BLE001
        return None, f"the npm read failed ({type(e).__name__})"


def check(refresh: bool = False) -> dict:
    """The whole tab payload, cached an hour. Every failed read is stated in the
    payload; the page prints unknowns as unknowns."""
    now = time.time()
    if not refresh and _CACHE["data"] and now - _CACHE["at"] < TTL:
        return {**_CACHE["data"], "cached": True}

    running, running_note = None, None
    try:
        _st, info = harnessctl._request("GET", harnessctl.HEALTH_PATH)
        running = (info or {}).get("version")
    except harnessctl.HarnessError as e:
        running_note = f"the host is not answering: {e}"

    disk, disk_note = disk_version()
    latest, latest_note = latest_v2()

    specs = harnessctl.plugin_specs()
    loaded, loaded_note = loaded_plugins(specs)
    plugins = []
    for spec in specs:
        name = spec.split("@")[0]
        pin = spec.partition("@")[2]
        npm, npm_note = npm_latest(name)
        got = loaded.get(name) or {}
        # "current" is the newest version in play — the pin, or what the host
        # actually loaded if it loaded something newer than the pin.
        cur = max(parse_ver(x) or (0, 0, 0) for x in (pin, got.get("version") or "0"))
        npm_v = parse_ver(npm)
        plugins.append({
            "name": name,
            "pinned": pin,
            "loaded": got.get("version"),
            "status": got.get("status"),
            "latest": npm,
            "update_available": bool(npm_v and npm_v > cur),
            "note": npm_note,
        })

    data = {
        "opencode": {
            "running": running, "running_note": running_note,
            "disk": disk, "disk_note": disk_note,
            "latest": latest, "latest_note": latest_note,
            "update_available": newer(latest, disk),
            # disk moved under the running service: a restart runs the new binary.
            "restart_needed": bool(running and disk and running != disk),
        },
        "plugins": plugins,
        "loaded_note": loaded_note,
        "checked_at": now,
        "verbs": ["upgrade", "restart", "plugins"],
    }
    _CACHE.update(at=now, data=data)
    return {**data, "cached": False}

