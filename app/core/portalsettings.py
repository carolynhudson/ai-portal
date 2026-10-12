"""The portal's own settings — the handful of facts the portal needs about this machine.

These are *not* the llama.cpp settings. The llama settings live in `personality.SETTINGS` and are
rendered by `settingrows.rows()`; `tests/test_settingsdoc.py` compares the generated reference
against that registry. This module is the other kind of setting: one path, the folder where the
operator keeps coding projects (`~/ai-projects`). It has no llama.cpp meaning; it is the projects
registry's root, and it is validated here rather than by the page that writes it.

Why a path gets a validator at all
----------------------------------
A projects root is the one setting that decides where an agent gets to write files. The failure
modes are all quiet:

- a **relative** path resolves against whatever working directory the service happens to start in,
  so the same saved setting points at two different folders on two boots;
- a path **outside the home** is a root the operator cannot see from their file manager, and an
  agent writing there writes where the operator is not looking;
- a path that lands on an **existing file** produces a create that fails with an error naming a
  project, not the setting;
- a path whose nearest existing ancestor is **not writable** produces a save that succeeds and a
  create that never does — the settings page says it is saved;
- a root **inside the portal's own home** puts generated projects next to the model library, the
  presets file and the password file.

So every refusal names the fact and what was expected, in the order a person can act on: what was
given, what the portal can accept, and the path it resolved to. Nothing here deletes, moves, or
rewrites a directory: validation is read-only, and creating the root happens as part of creating
a project, where the caller has to ask for it.
"""
from __future__ import annotations

import os
from typing import Any

from . import db

#: Every portal setting the page can write, with the shape the page needs. `kind` is the control;
#: `default` is what the portal uses when nothing is stored.
PORTAL_SETTINGS: dict[str, dict[str, Any]] = {
    "projects_root": {
        "kind": "text",
        "default": "~/ai-projects",
        "help": "The folder that holds the coding projects the harness works on. Projects are "
                "sub-folders of it, and the harness is launched inside one.",
    },
}

#: The root the portal is willing to use, expanded. Everything else is refused with a reason.
_MAX_DEPTH = 24


def home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def portal_home() -> str:
    """The portal's own home (`~/ai` by default) — the model library, presets, and secrets live there."""
    from . import paths
    return os.path.realpath(str(paths.AI_ROOT))


def _resolve(value: str) -> str:
    """`~` and `$VAR` expanded, made absolute, symlinks followed.

    A relative value is *not* made absolute against the process cwd here; the caller refuses it
    first, because the answer would depend on how the service happened to start.
    """
    return os.path.realpath(os.path.expandvars(os.path.expanduser(value)))


def validate_projects_root(value: Any) -> dict[str, Any]:
    """Can this string be the projects root? Answers with the reason, never with a traceback.

    Read-only. Never creates, moves, deletes, or chmods anything.
    """
    out: dict[str, Any] = {"ok": False, "value": value if isinstance(value, str) else None}

    if not isinstance(value, str):
        out["error"] = "the projects root must be a path string"
        return out

    raw = value.strip()
    if not raw:
        out["error"] = "the projects root cannot be empty"
        return out

    if not raw.startswith(("/", "~", "$")):
        out["error"] = (
            f"{raw!r} is a relative path. The portal service starts in its own working directory, "
            f"so a relative root resolves to a folder you cannot see from the page. Give an "
            f"absolute path, or one starting with ~ (for example ~/ai-projects)."
        )
        return out

    if "\x00" in raw:
        out["error"] = "the projects root contains a null byte"
        return out

    if raw.count("..") > _MAX_DEPTH:
        out["error"] = "the projects root has more path traversal than this accepts"
        return out

    try:
        resolved = _resolve(raw)
    except (OSError, ValueError) as exc:
        out["error"] = f"{raw!r} cannot be resolved to a path: {type(exc).__name__}: {exc}"
        return out
    out["path"] = resolved

    h = home()
    if resolved != h and not resolved.startswith(h + os.sep):
        out["error"] = (
            f"{resolved} is outside your home directory ({h}). The projects root is where the "
            f"harness writes files; the portal will only point an agent at a folder inside your "
            f"home."
        )
        return out

    ph = portal_home()
    if resolved == ph:
        out["error"] = (
            f"{resolved} is the portal's own home — the model library, presets file, database, "
            f"and password file live under it. Choose a different folder for projects "
            f"(~/ai-projects is the default)."
        )
        return out
    if resolved.startswith(ph + os.sep):
        out["error"] = (
            f"{resolved} is inside the portal's own home ({ph}). Projects next to the model "
            f"library and the secrets is a mess nobody can untangle; choose a folder outside it."
        )
        return out

    # The filesystem probes below are the part of this that can raise: a symlink loop, a name
    # that is too long, an I/O error on a mount that has gone away. All of them are answers the
    # page can show, not tracebacks the request has to die with.
    try:
        existing = resolved
        while not os.path.exists(existing):
            parent = os.path.dirname(existing)
            if parent == existing:
                break
            existing = parent

        if os.path.exists(resolved) and not os.path.isdir(resolved):
            out["error"] = (
                f"{resolved} exists and is not a directory. Point the projects root at a folder, "
                f"or move that file out of the way."
            )
            return out

        if not os.path.isdir(existing):
            out["error"] = (
                f"{existing} exists but is not a directory, so projects cannot go inside it"
            )
            return out

        if not os.access(existing, os.W_OK | os.X_OK):
            out["error"] = (
                f"{existing} is not writable by the portal's user, so a project created under "
                f"{resolved} would fail after the setting was saved."
            )
            return out
    except OSError as exc:
        out["error"] = f"{resolved} cannot be inspected: {type(exc).__name__}: {exc}"
        return out

    out["ok"] = True
    out["exists"] = os.path.isdir(resolved)
    out["will_create"] = not out["exists"]
    out["nearest_existing"] = existing
    return out


def projects_root(default: str | None = None) -> str:
    """The stored projects root, expanded. The default is always valid, so this cannot fail."""
    stored = db.get_setting("projects_root")
    value = default if default is not None else (stored or PORTAL_SETTINGS["projects_root"]["default"])
    return _resolve(str(value))


def set_value(key: str, value: Any) -> dict[str, Any]:
    """Validate and store one portal setting. Refusals never reach the database."""
    if key not in PORTAL_SETTINGS:
        return {"ok": False, "key": key,
                "error": f"unknown portal setting {key!r}; the page can write "
                         f"{', '.join(sorted(PORTAL_SETTINGS))}"}

    if key == "projects_root":
        v = validate_projects_root(value)
        if not v["ok"]:
            return {"ok": False, "key": key, "error": v["error"], "path": v.get("path")}
        db.set_setting(key, str(value).strip())
        return {"ok": True, "key": key, "value": str(value).strip(), "path": v["path"]}

    return {"ok": False, "key": key, "error": f"{key} has no validation rule, so the portal will not write it"}


def view() -> dict[str, Any]:
    """Everything the settings page needs: each key, its control, its stored and effective values."""
    stored = db.current_settings()
    out: dict[str, Any] = {}
    for key, spec in PORTAL_SETTINGS.items():
        effective = stored.get(key, spec["default"])
        out[key] = {
            "kind": spec["kind"],
            "default": spec["default"],
            "help": spec["help"],
            "value": effective,
            "path": _resolve(str(effective)),
            "exists": os.path.isdir(_resolve(str(effective))),
        }
    return out


def writable_keys() -> tuple[str, ...]:
    return tuple(PORTAL_SETTINGS)


def is_writable(key: str) -> bool:
    return key in PORTAL_SETTINGS
