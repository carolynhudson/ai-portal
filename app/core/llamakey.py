"""Optional API key for the llama.cpp router — the cheap way to stop a
LAN-exposed router being anonymous.

Opening the router to the network (``LLAMA_HOST=0.0.0.0``) also exposes its
Web UI, ``/props`` and every loaded model to anything that can route to the
box. llama.cpp supports ``--api-key-file`` (one key per line), so the portal
can hand out one shared key without putting a proxy in front:

    Authorization: Bearer <key>      # OpenAI-compatible clients
    ?api_key=<key>                   # web UI / simple clients

Design notes
------------
* The key lives in ``~/ai/state/portal/llama-api-key.txt`` (0600), never in an
  env file or a unit line — env files end up in logs, screenshots and
  ``systemctl show`` output; a *path* to a file is harmless there.
* ``~/ai/config/llamacpp.env`` stores only ``LLAMA_API_KEY_FILE=<path>``; the
  generated router wrapper appends ``--api-key-file`` when that path exists.
* Nothing here restarts the router: applying it is an explicit confirmed
  action, because the agent session riding the router would drop.
* :func:`live_state` probes the running router (with and without the key) so
  the UI can tell "configured" from "actually enforced".
"""
from __future__ import annotations

import httpx
import os
import secrets
import time

from . import paths, serviceconf

KEY_FILE = paths.PORTAL_STATE / "llama-api-key.txt"
ENV_KEY = "LLAMA_API_KEY_FILE"


def _env() -> dict[str, str]:
    return serviceconf.read_service_env("llamacpp")


def file_present() -> bool:
    try:
        return os.path.isfile(_env().get(ENV_KEY, ""))
    except OSError:
        return False


def configured() -> bool:
    """Does the launch config point at an existing key file? (Effective only
    after the router restarts — see :func:`live_state`.)"""
    return file_present()


_key_cache: tuple[float, str | None, dict] = (0.0, None, {})


def _invalidate() -> None:
    """Drop the memo — called by anything that creates, rotates or deletes the
    key, so a probe right after generate() never sees the previous value."""
    global _key_cache
    _key_cache = (0.0, None, {})


def reveal() -> str | None:
    """The current key, or None. Cached for 5 s: probes call this on every
    dashboard refresh and the file never changes on that timescale (except via
    generate/revoke below, which invalidate the cache)."""
    global _key_cache
    now = time.monotonic()
    if now - _key_cache[0] < 5.0:
        return _key_cache[1]
    key = KEY_FILE.read_text().strip() if KEY_FILE.is_file() else None
    _key_cache = (now, key, {})
    return key


def request_headers() -> dict:
    """``Authorization`` header for the portal's own calls to the router —
    empty when no key is configured, so unauthenticated routers keep working
    and the dashboard does not 401 itself out after a key is enabled."""
    if not configured():
        return {}
    key = reveal()
    return {"Authorization": f"Bearer {key}"} if key else {}


def web_ui_url(base: str) -> str:
    """Router Web UI URL a human can paste — carries ``?api_key=`` when the
    router demands one, because the built-in UI has no login form."""
    key = reveal() if configured() else None
    return f"{base}/?api_key={key}" if key else f"{base}/"


def generate(force: bool = False) -> dict:
    """Create the key file (0600) and point the router launch env at it.

    Idempotent: an existing key is kept unless ``force`` — rotating silently
    breaks every client that already has the old one.
    """
    if KEY_FILE.is_file() and not force:
        key, created = KEY_FILE.read_text().strip(), False
    else:
        key = secrets.token_urlsafe(32)
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key + "\n", encoding="utf-8")
        os.chmod(KEY_FILE, 0o600)
        created = True
    _invalidate()
    env = _env()
    env[ENV_KEY] = str(KEY_FILE)
    serviceconf.write_service_env("llamacpp", env)
    return {"ok": True, "configured": True, "created": created, "key": key,
            "key_file": str(KEY_FILE), "needs_restart": True,
            "unit": serviceconf.SERVICE_CONF["llamacpp"]["unit"]}


def revoke(remove_file: bool = True) -> dict:
    """Drop the launch-env pointer (and by default the key file): the router
    returns to unauthenticated on its next restart."""
    _invalidate()
    env = _env()
    env.pop(ENV_KEY, None)
    serviceconf.write_service_env("llamacpp", env)
    removed = False
    if remove_file and KEY_FILE.is_file():
        KEY_FILE.unlink()
        removed = True
    return {"ok": True, "configured": False, "file_removed": removed,
            "needs_restart": True,
            "unit": serviceconf.SERVICE_CONF["llamacpp"]["unit"]}


def live_state(timeout: float = 3.0) -> dict:
    """Ask the *running* router whether it demands a key.

    ``/props`` is unauthenticated-but-sensitive: 200 without a key means the
    key is not in force (router not restarted yet, or the wrapper has no
    ``--api-key-file``); 401 without and 200 with means it is.
    """
    base = serviceconf.base_url("llamacpp", default_port=8080)
    key = reveal()
    out: dict = {"reachable": False, "enforced": None,
                 "anonymous_ok": None, "key_ok": None, "base": base}
    try:
        anon = httpx.get(f"{base}/props", timeout=timeout)
        out["reachable"] = True
        out["anonymous_ok"] = anon.status_code == 200
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
        return out
    if out["anonymous_ok"]:
        out["enforced"] = False
        return out
    if key:
        try:
            withkey = httpx.get(f"{base}/props", headers={"Authorization": f"Bearer {key}"},
                                timeout=timeout)
            out["key_ok"] = withkey.status_code == 200
            out["enforced"] = bool(out["key_ok"])
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:200]
    else:
        # refuses anonymous calls but we hold no key: someone else set it
        out["enforced"] = True
    return out


def status() -> dict:
    """Everything the Settings page needs about router authentication."""
    cfg = configured()
    return {
        "configured": cfg,
        "key_file": str(KEY_FILE),
        "env_key": ENV_KEY,
        "file_present": KEY_FILE.is_file(),
        "key_preview": (reveal() or "")[:6] + "…" if cfg else None,
        "live": live_state(),
        "note": ("clients send: Authorization: Bearer <key> (or ?api_key=<key>); "
                 "restart the router to apply"),
    }
