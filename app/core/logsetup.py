"""Logging setup: rotating file log + stderr, level chosen at launch.

Why: the portal runs as a user service, so without this the only trace of a
bad request is the journal (which loses history on rotation and cannot be
tailing-friendly across restarts). One rotating file under ``~/ai/logs`` plus
stderr (→ journal) covers both "what happened yesterday" and
``journalctl -u ai-portal -f``.

Level resolution (first win): explicit ``level=`` argument →
``AI_PORTAL_LOG_LEVEL`` (env file / unit / shell) → ``info``. Bad values fall
back to ``info`` rather than raising, because this runs at import time in
tests and on a service restart where a crash would be the worst outcome.

Usage::

    from .core import logsetup
    log = logsetup.setup()          # once, from create_app()
    log.info("portal starting on %s", addr)
    ...
    logsetup.get(__name__).warning("...")
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import paths

LOGGER_NAME = "aiportal"
DEFAULT_LEVEL = "info"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
_Levels = ("critical", "error", "warning", "info", "debug", "trace")

_configured = False


def resolve_level(level: str | None = None) -> int:
    """Map a level name (or number) to a logging level; unknown → INFO.

    ``trace`` is accepted as an alias for a level below DEBUG (1): useful when
    probing why a probe returns nothing, without inventing a new logger level.
    """
    raw = (level or os.environ.get("AI_PORTAL_LOG_LEVEL") or DEFAULT_LEVEL)
    if isinstance(raw, int):
        return raw
    name = str(raw).strip().lower()
    if name == "trace":
        return 1
    return getattr(logging, name.upper(), None) or logging.INFO


def log_file() -> Path:
    return paths.LOGS / "portal.log"


def setup(level: str | int | None = None, log_path: Path | None = None,
          console: bool = True) -> logging.Logger:
    """Configure (idempotently) and return the portal logger.

    The file handler is skipped when the log directory cannot be created
    (read-only HOME in a sandbox, tests) — logging must never be the reason
    the portal fails to start.
    """
    global _configured
    log = logging.getLogger(LOGGER_NAME)
    lvl = resolve_level(level)
    log.setLevel(lvl)
    log.propagate = False
    if _configured:
        # a later call may only raise/lower the level, not duplicate handlers
        return log

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    target = log_path or log_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
        fh.setFormatter(fmt)
        fh.setLevel(lvl)
        log.addHandler(fh)
    except OSError as e:  # pragma: no cover - environment dependent
        if console:
            print(f"[ai-portal] file logging disabled ({e})", file=sys.stderr,
                  flush=True)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.setLevel(lvl)
        log.addHandler(ch)

    # uvicorn's own access/error logs: keep them, but quiet the per-request
    # access noise (the dashboard polls several endpoints every few seconds).
    uv = logging.getLogger("uvicorn")
    uv.setLevel(logging.WARNING if lvl > logging.INFO else lvl)
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.WARNING if lvl > logging.DEBUG else logging.INFO)

    _configured = True
    log.info("logging at %s -> %s",
             logging.getLevelName(lvl), target if log.handlers else "(console only)")
    return log


def get(name: str | None = None) -> logging.Logger:
    """Child logger; ``name`` is usually ``__name__`` (app.core.foo)."""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    short = name[4:] if name.startswith("app.") else name
    return logging.getLogger(f"{LOGGER_NAME}.{short}")


def levels() -> list[str]:
    return list(_Levels)
